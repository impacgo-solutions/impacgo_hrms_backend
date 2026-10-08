"""End-to-end WRITE workflow test for one tenant (default: impacgo-solutions).

Every role raises real requests through the API, the requester's actual
Reporting Manager (read from core_employees) approves/rejects them, and the
test verifies each hop:

    requester creates -> appears in requester's list -> appears in approver's
    list -> approver gets a notification -> requester cannot self-approve ->
    approver decides -> status persisted -> requester sees decision + gets a
    notification -> a second decision is refused (409)

Plus RBAC probes (an unrelated IC trying to decide someone else's request),
leave-balance bookkeeping, overlap protection, and self-service writes
(loans, tax declarations, skills, recognitions, OKRs, profile, prefs).

Every record created carries the marker "[SMOKE]" in its free-text field so
it can be found and removed later; ids are written to e2e_report.json.
This DOES write to the database -- run it against a test DB only.

    cd backend
    venv\\Scripts\\python tests\\e2e_workflows.py [--tenant SLUG] [--base-url URL]
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

import requests
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings  # noqa: E402

# The test accounts' password comes from the environment only -- never
# committed (it logs in real accounts on the shared database).
PASSWORD = os.environ.get("SMOKE_PASSWORD") or sys.exit(
    "Set SMOKE_PASSWORD to the test accounts' password before running."
)
# Accounts whose password differs from the shared one: "email=pw,email=pw"
# (environment only, like SMOKE_PASSWORD).
PASSWORD_OVERRIDES = {
    k.strip().lower(): v
    for k, _, v in (item.partition("=") for item in os.environ.get("SMOKE_PASSWORDS", "").split(",") if "=" in item)
}
TAG = "[SMOKE]"
TIMEOUT = 90
RUN = uuid.uuid4().hex[:6]
SKIP_EMAILS = {e.strip().lower() for e in os.environ.get("SMOKE_SKIP", "nagasai@impacgo.com").split(",")}

results: list[dict] = []
created: list[dict] = []


def record(flow, actor, step, ok, status=None, detail="", severity="HIGH"):
    results.append(
        {"flow": flow, "actor": actor, "step": step, "pass": bool(ok),
         "status": status, "detail": str(detail)[:400], "severity": None if ok else severity}
    )
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} [{flow}] {actor}: {step} ({status}) {'' if ok else str(detail)[:200]}")


class Client:
    def __init__(self, base, email, role, emp):
        self.base, self.email, self.role, self.emp = base, email, role, emp
        self.name = email.split("@")[0]
        self.s = requests.Session()
        self.ok = False

    def login(self):
        r = self.s.post(f"{self.base}/api/auth/login", json={"email": self.email, "password": PASSWORD_OVERRIDES.get(self.email.lower(), PASSWORD)}, timeout=TIMEOUT)
        if r.status_code == 200:
            self.s.headers["Authorization"] = "Bearer " + r.json()["access_token"]
            self.ok = True
        return r

    def req(self, method, path, **kw):
        try:
            r = self.s.request(method, f"{self.base}{path}", timeout=TIMEOUT, **kw)
        except requests.RequestException as exc:
            class R:  # minimal stand-in
                status_code = None
                text = exc.__class__.__name__
                def json(self):
                    return None
            return R()
        return r

    def get(self, path, **params):
        return self.req("GET", path, params=params)

    def post(self, path, body):
        return self.req("POST", path, json=body)

    def patch(self, path, body):
        return self.req("PATCH", path, json=body)

    def put(self, path, body):
        return self.req("PUT", path, json=body)

    def delete(self, path):
        return self.req("DELETE", path)

    def unread(self):
        r = self.get("/api/notifications/unread-count")
        try:
            j = r.json()
            return j.get("count", j.get("unread_count")) if isinstance(j, dict) else j
        except Exception:
            return None


def as_list(resp):
    try:
        j = resp.json()
    except Exception:
        return []
    if isinstance(j, dict):
        for k in ("items", "data", "results", "rows", "records"):
            if isinstance(j.get(k), list):
                return j[k]
        return []
    return j if isinstance(j, list) else []


def find(resp, rid):
    return next((x for x in as_list(resp) if str(x.get("id")) == str(rid)), None)


def body_json(resp):
    try:
        return resp.json()
    except Exception:
        return {}


# ---------------------------------------------------------------- discovery
def discover(tenant):
    eng = create_engine(settings.database_url)
    S = f'"{tenant}"'
    with eng.connect() as c:
        rows = c.execute(text(f"""
            SELECT pu.email, e.id, e.first_name || ' ' || coalesce(e.last_name,''), e.reporting_manager_id,
                   e.dotted_line_manager_id, r.name
            FROM public.users pu
            JOIN {S}.core_employees e ON e.id = coalesce(pu.employee_id, pu.id)
            JOIN {S}.core_users cu ON cu.employee_id = e.id AND cu.status = 'active'
            JOIN {S}.core_user_roles ur ON ur.user_id = cu.id
            JOIN {S}.core_roles r ON r.id = ur.role_id
            WHERE pu.tenant_slug = :t AND pu.is_active
            ORDER BY r.name, pu.email"""), {"t": tenant}).all()
    people = {}
    for email, eid, name, rm, dl, role in rows:
        people[str(eid)] = {"email": email, "emp": str(eid), "name": name.strip(),
                            "rm": str(rm) if rm else None, "dl": str(dl) if dl else None, "role": role}
    return people


# ---------------------------------------------------------------- generic approval flow
def approval_flow(flow, requester, approver, outsider, create_path, create_body, list_path,
                  patch_path, decision, created_note=None, list_params=None, extra_check=None,
                  admin_override=False):
    """Runs the standard create -> visible -> self-approve blocked -> decide -> verify chain."""
    actor = f"{requester.role} ({requester.name})"
    before_appr = approver.unread() if approver else None
    r = requester.post(create_path, create_body)
    if r.status_code not in (200, 201):
        record(flow, actor, f"create via POST {create_path}", False, r.status_code, r.text)
        return None
    obj = body_json(r)
    rid = obj.get("id")
    created.append({"flow": flow, "path": create_path, "id": rid, "by": requester.email, "note": created_note})
    record(flow, actor, "create", bool(rid), r.status_code, "" if rid else f"no id in response: {r.text}")
    if not rid:
        return None

    lp = list_params or {}
    record(flow, actor, "visible in requester's own list", find(requester.get(list_path, **lp), rid) is not None,
           200, f"{rid} not returned by GET {list_path}", "MEDIUM")

    if approver is None:
        record(flow, actor, "has an approver (reporting manager)", False, None,
               "requester has no reporting manager -- nobody can approve this", "MEDIUM")
        return rid

    appr_actor = f"{approver.role} ({approver.name})"
    if not approver.ok:
        record(flow, appr_actor, "reporting manager can log in to decide", False, 401,
               f"{approver.email} cannot log in with the shared password -- request stays pending", "HIGH")
        return rid
    record(flow, appr_actor, "visible in approver's list", find(approver.get(list_path, **lp), rid) is not None,
           200, f"{rid} not visible to approver via GET {list_path}", "HIGH")
    after_appr = approver.unread()
    if isinstance(before_appr, int) and isinstance(after_appr, int):
        record(flow, appr_actor, "approver notified of new request", after_appr > before_appr, None,
               f"unread-count {before_appr} -> {after_appr}", "MEDIUM")

    sr = requester.patch(patch_path.format(id=rid), {"status": "approved", "decision_notes": TAG + " self"})
    record(flow, actor, "self-approval blocked", sr.status_code in (403, 409), sr.status_code,
           f"requester could approve own request: {sr.text}", "CRITICAL")

    if outsider is not None:
        orq = outsider.patch(patch_path.format(id=rid), {"status": "sent_back", "decision_notes": TAG + " outsider"})
        ok = orq.status_code in (403, 404)
        record(flow, f"{outsider.role} ({outsider.name})",
               "unrelated employee (not in reporting chain) blocked from deciding", ok, orq.status_code,
               f"outsider changed someone else's request: {orq.text}", "CRITICAL")
        if not ok:
            # outsider's action went through; re-read state, approver may now be blocked
            pass

    before_req = requester.unread()
    d = approver.patch(patch_path.format(id=rid), {"status": decision, "decision_notes": f"{TAG} {decision}"})
    record(flow, appr_actor, f"{decision} by reporting manager", d.status_code == 200, d.status_code, d.text)
    if d.status_code != 200:
        return rid
    got = str(body_json(d).get("status", "")).lower()
    record(flow, appr_actor, f"response status == {decision}", got == decision, d.status_code,
           f"server returned status={got!r}", "MEDIUM")

    row = find(requester.get(list_path, **lp), rid)
    seen = str((row or {}).get("status", "")).lower()
    record(flow, actor, "requester sees persisted decision", seen == decision, None,
           f"requester sees status={seen!r}", "HIGH")
    after_req = requester.unread()
    if isinstance(before_req, int) and isinstance(after_req, int):
        record(flow, actor, "requester notified of decision", after_req > before_req, None,
               f"unread-count {before_req} -> {after_req}", "MEDIUM")

    if admin_override:
        # work entries / timesheets are auto-approved; PATCH is an admin override, re-deciding is by design
        return rid
    again = approver.patch(patch_path.format(id=rid), {"status": "rejected" if decision == "approved" else "approved",
                                                        "decision_notes": TAG + " again"})
    record(flow, appr_actor, "second decision refused", again.status_code in (400, 409), again.status_code,
           f"already-decided request was decided again: {again.text}", "HIGH")
    if extra_check:
        extra_check(rid)
    return rid


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="impacgo-solutions")
    ap.add_argument("--base-url", default="http://localhost:8000")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    people = discover(args.tenant)
    clients: dict[str, Client] = {}
    for eid, p in people.items():
        c = Client(base, p["email"], p["role"], eid)
        r = c.login()
        record("auth", f"{p['role']} ({c.name})", "login with shared password", r.status_code == 200, r.status_code,
               "" if r.status_code == 200 else r.text, "HIGH")
        clients[eid] = c

    # One requester per role, plus an IC who reports to another IC (exercises IC-as-approver).
    by_role: dict[str, Client] = {}
    for eid, p in people.items():
        if p["email"].lower() in SKIP_EMAILS:
            continue
        by_role.setdefault(p["role"], clients[eid])
    requesters = list(by_role.values())
    for eid, p in people.items():
        rm = people.get(p["rm"] or "")
        if rm and rm["role"] == p["role"] and clients[eid] not in requesters and p["email"].lower() not in SKIP_EMAILS:
            requesters.append(clients[eid])
            break

    def approver_of(c):
        rm = people[c.emp]["rm"]
        return clients.get(rm) if rm else None

    def outsider_of(c):
        """Someone who is neither the requester nor anywhere in their reporting chain, and not Owner."""
        chain, cur = set(), people[c.emp]["rm"]
        while cur and cur not in chain:
            chain.add(cur)
            cur = people.get(cur, {}).get("rm")
        for eid, p in people.items():
            if eid != c.emp and eid not in chain and "Owner" not in p["role"] and p["email"].lower() not in SKIP_EMAILS:
                return clients[eid]
        return None

    today = date.today()
    print(f"\nTenant {args.tenant}: {len(people)} users; requesters: {[r.email for r in requesters]}\n")

    for i, rq in enumerate(requesters):
        appr, out = approver_of(rq), outsider_of(rq)
        actor = f"{rq.role} ({rq.name})"
        print(f"== {actor} -> approver {appr.email if appr else None}, outsider {out.email if out else None}")

        # ---------------- LEAVE ----------------
        types = as_list(rq.get("/api/leave-types"))
        record("leave", actor, "leave types available", bool(types), None, "GET /api/leave-types returned nothing")
        balances = as_list(rq.get("/api/leave-balances", employee_id=rq.emp))
        mine = [b for b in balances if str(b.get("employee_id", rq.emp)) == rq.emp]
        record("leave", actor, "leave balances returned for self", bool(mine), None,
               "no leave-balance rows for this employee -- UI will fall back to client-computed balances", "MEDIUM")
        def bal_of(name):
            for b in as_list(rq.get("/api/leave-balances", employee_id=rq.emp)):
                if str(b.get("employee_id", rq.emp)) == rq.emp and (b.get("leave_type_name") or b.get("leave_type") or b.get("name")) == name:
                    return b
            return None
        type_name = None
        for b in mine:
            avail = b.get("remaining", b.get("available", b.get("balance")))
            if isinstance(avail, (int, float)) and avail >= 1:
                type_name = b.get("leave_type_name") or b.get("leave_type") or b.get("name")
                break
        if not type_name and types:
            type_name = types[0].get("name")
        # Stay inside the current fiscal year (Apr-Mar): a leave dated after
        # it has no allocation, so the balance check below can't move.
        base_day = date(2027, 1, 4) + timedelta(days=14 * i + (hash(RUN) % 3))
        while base_day.weekday() >= 5:
            base_day += timedelta(days=1)
        if type_name:
            before_bal = bal_of(type_name)
            body = {"employee_id": rq.emp, "leave_type_name": type_name, "from_date": base_day.isoformat(),
                    "to_date": base_day.isoformat(), "days": 1, "reason": f"{TAG} approve-path {RUN}"}

            def leave_balance_check(rid, tn=type_name, before=before_bal):
                after = bal_of(tn)
                if before and after:
                    u0 = before.get("used", before.get("taken"))
                    u1 = after.get("used", after.get("taken"))
                    if isinstance(u0, (int, float)) and isinstance(u1, (int, float)):
                        record("leave", actor, "balance 'used' +1 after approval", abs((u1 - u0) - 1) < 0.01, None,
                               f"used {u0} -> {u1}", "HIGH")

            approval_flow("leave", rq, appr, out, "/api/leave-requests", body, "/api/leave-requests",
                          "/api/leave-requests/{id}", "approved", extra_check=leave_balance_check)

            dup = rq.post("/api/leave-requests", body)
            record("leave", actor, "overlapping leave refused", dup.status_code == 409, dup.status_code,
                   f"duplicate/overlapping leave accepted: {dup.text}", "HIGH")
            if dup.status_code in (200, 201):
                created.append({"flow": "leave", "path": "/api/leave-requests", "id": body_json(dup).get("id"), "by": rq.email, "note": "duplicate"})

            d2 = base_day + timedelta(days=7)
            body2 = {**body, "from_date": d2.isoformat(), "to_date": d2.isoformat(), "reason": f"{TAG} reject-path {RUN}"}
            approval_flow("leave", rq, appr, None, "/api/leave-requests", body2, "/api/leave-requests",
                          "/api/leave-requests/{id}", "rejected")

            bad = rq.post("/api/leave-requests", {**body, "from_date": (d2 + timedelta(days=3)).isoformat(),
                                                   "to_date": (d2 + timedelta(days=1)).isoformat()})
            record("leave", actor, "to_date before from_date refused", bad.status_code in (400, 409, 422), bad.status_code,
                   f"accepted a leave ending before it starts: {bad.text}", "MEDIUM")
            if bad.status_code in (200, 201):
                created.append({"flow": "leave", "path": "/api/leave-requests", "id": body_json(bad).get("id"), "by": rq.email, "note": "reversed dates"})

        # ---------------- ATTENDANCE REGULARIZATION ----------------
        reg_day = today - timedelta(days=10 + i)
        while reg_day.weekday() >= 5:
            reg_day -= timedelta(days=1)
        approval_flow("regularization", rq, appr, None, "/api/attendance/regularizations",
                      {"employee_id": rq.emp, "attendance_date": reg_day.isoformat(), "reason": f"{TAG} {RUN}",
                       "requested_in": f"{reg_day.isoformat()}T09:30:00", "requested_out": f"{reg_day.isoformat()}T18:30:00"},
                      "/api/attendance/regularizations", "/api/attendance/regularizations/{id}", "approved")

        # ---------------- OVERTIME ----------------
        approval_flow("overtime", rq, appr, None, "/api/overtime-requests",
                      {"employee_id": rq.emp, "work_date": (today - timedelta(days=2)).isoformat(), "hours": 1.5,
                       "reason": f"{TAG} {RUN}"},
                      "/api/overtime-requests", "/api/overtime-requests/{id}", "approved")

        # ---------------- WORK ENTRY ----------------
        projects = as_list(rq.get("/api/projects", employee_id=rq.emp)) or as_list(rq.get("/api/projects"))
        pid = projects[0]["id"] if projects else None
        approval_flow("work-entry", rq, appr, None, "/api/work-entries",
                      {"employee_id": rq.emp, "entry_date": (today - timedelta(days=1)).isoformat(), "project_id": pid,
                       "task": f"{TAG} task {RUN}", "category": "Development", "start_time": "10:00", "end_time": "12:00",
                       "hours": 2, "description": f"{TAG} {RUN}", "is_billable": False},
                      "/api/work-entries", "/api/work-entries/{id}", "approved", admin_override=True)

        # ---------------- TIMESHEET ----------------
        wk = today - timedelta(days=today.weekday() + 7 * (3 + i))
        approval_flow("timesheet", rq, appr, None, "/api/timesheets",
                      {"employee_id": rq.emp, "week_start": wk.isoformat(), "total_hours": 40, "billable_hours": 0},
                      "/api/timesheets", "/api/timesheets/{id}", "rejected", admin_override=True)

        # ---------------- TRAVEL ----------------
        t0 = date(2027, 5, 3) + timedelta(days=14 * i)
        approval_flow("travel", rq, appr, None, "/api/travel-requests",
                      {"employee_id": rq.emp, "purpose": f"{TAG} {RUN}", "destination": "Hyderabad",
                       "from_date": t0.isoformat(), "to_date": (t0 + timedelta(days=2)).isoformat(),
                       "travel_mode": "Flight", "estimated_cost": 12000},
                      "/api/travel-requests", "/api/travel-requests/{id}", "approved")

        # ---------------- EXPENSE REPORT ----------------
        approval_flow("expense-report", rq, appr, None, "/api/expense-reports",
                      {"employee_id": rq.emp, "reference": f"{TAG}-{RUN}-{i}", "submitted_date": today.isoformat(),
                       "items": [{"date": (today - timedelta(days=3)).isoformat(), "category": "Meals",
                                  "description": f"{TAG} lunch", "amount": 450}]},
                      "/api/expense-reports", "/api/expense-reports/{id}", "approved")

        # ---------------- REIMBURSEMENT ----------------
        approval_flow("reimbursement", rq, appr, None, "/api/reimbursements",
                      {"employee_id": rq.emp, "category": "Internet", "amount": 999,
                       "expense_date": (today - timedelta(days=4)).isoformat()},
                      "/api/reimbursements", "/api/reimbursements/{id}", "approved")

        # ---------------- ASSET REQUEST ----------------
        approval_flow("asset-request", rq, appr, None, "/api/asset-requests",
                      {"employee_id": rq.emp, "asset_type": "Laptop", "justification": f"{TAG} {RUN}"},
                      "/api/asset-requests", "/api/asset-requests/{id}", "rejected")

        # ---------------- SELF-SERVICE (no approval) ----------------
        for flow, path, body, lp in [
            ("loan", "/api/loans", {"employee_id": rq.emp, "loan_type": "Salary Advance", "principal_amount": 1000,
                                    "emi_amount": 500, "outstanding_balance": 1000}, "/api/loans"),
            ("tax-declaration", "/api/tax-declarations", {"employee_id": rq.emp, "fiscal_year": "2026-27",
                                                          "tax_regime": "new", "hra_claimed": 0, "section_80c": 0},
             "/api/tax-declarations"),
        ]:
            r = rq.post(path, body)
            ok = r.status_code in (200, 201)
            record(flow, actor, f"create via POST {path}", ok, r.status_code, r.text)
            if ok:
                rid = body_json(r).get("id")
                created.append({"flow": flow, "path": path, "id": rid, "by": rq.email})
                record(flow, actor, "visible in own list", find(rq.get(lp), rid) is not None, 200,
                       f"{rid} not in GET {lp}", "MEDIUM")

        r = rq.put("/api/skill-ratings", {"employee_id": rq.emp, "skill": f"{TAG} Python", "level": "Advanced"})
        record("skills", actor, "upsert own skill rating", r.status_code in (200, 201), r.status_code, r.text, "MEDIUM")

        colleague = appr or out
        if colleague:
            r = rq.post("/api/recognitions", {"employee_id": colleague.emp, "badge": "Team Player",
                                              "reason": f"{TAG} {RUN}", "given_on": today.isoformat()})
            ok = r.status_code in (200, 201)
            record("recognition", actor, "give recognition to colleague", ok, r.status_code, r.text, "MEDIUM")
            if ok:
                created.append({"flow": "recognition", "path": "/api/recognitions", "id": body_json(r).get("id"), "by": rq.email})

        r = rq.post("/api/okrs", {"level": "Individual", "title": f"{TAG} OKR {RUN}", "owner": people[rq.emp]["name"]})
        ok = r.status_code in (200, 201)
        record("okr", actor, "create individual OKR", ok, r.status_code, r.text, "MEDIUM")
        if ok:
            created.append({"flow": "okr", "path": "/api/okrs", "id": body_json(r).get("id"), "by": rq.email})

        # Profile round-trip: write the same nationality back (no data change) to test the write path.
        prof = body_json(rq.get(f"/api/employees/{rq.emp}"))
        nat = prof.get("nationality") if isinstance(prof, dict) else None
        r = rq.patch(f"/api/employees/{rq.emp}/profile", {"nationality": nat or "Indian"})
        record("profile", actor, "update own profile (no-op value)", r.status_code == 200, r.status_code, r.text, "MEDIUM")

        # Other employee's profile -- an IC must not edit someone else's personal data.
        if out and "IC" in rq.role:
            victim = out.emp
            vprof = body_json(rq.get(f"/api/employees/{victim}"))
            vnat = vprof.get("nationality") if isinstance(vprof, dict) else None
            r = rq.patch(f"/api/employees/{victim}/profile", {"nationality": vnat or "Indian"})
            record("profile", actor, "IC blocked from editing another employee's profile", r.status_code in (403, 404),
                   r.status_code, f"IC edited {out.email}'s profile: {r.text[:150]}", "CRITICAL")

        # Clock in / out (today)
        rec_before = rq.get("/api/attendance/records", employee_id=rq.emp)
        r = rq.post("/api/attendance/clock", {"employee_id": rq.emp, "action": "check_in", "work_mode": "WFO"})
        if r.status_code in (200, 201):
            record("attendance", actor, "clock in", True, r.status_code)
            br = rq.post("/api/attendance/breaks/start", {"employee_id": rq.emp})
            record("attendance", actor, "start break", br.status_code in (200, 201), br.status_code, br.text, "MEDIUM")
            be = rq.post("/api/attendance/breaks/end", {"employee_id": rq.emp})
            record("attendance", actor, "end break", be.status_code in (200, 201), be.status_code, be.text, "MEDIUM")
            co = rq.post("/api/attendance/clock", {"employee_id": rq.emp, "action": "check_out"})
            record("attendance", actor, "clock out", co.status_code in (200, 201), co.status_code, co.text)
        elif r.status_code in (400, 409):
            record("attendance", actor, "clock in (already clocked in/out today -- not a failure)", True, r.status_code, r.text)
        else:
            record("attendance", actor, "clock in", False, r.status_code, r.text)
        # HR / Owner may mark attendance for others by design (like leave on
        # behalf, below); probing them would clock a real colleague in.
        if out and not ("HR" in rq.role or "Owner" in rq.role):
            r = rq.post("/api/attendance/clock", {"employee_id": out.emp, "action": "check_in"})
            record("attendance", actor, "cannot clock in on behalf of another employee", r.status_code in (403, 404),
                   r.status_code, f"clocked in for {out.email}: {r.text[:150]}", "CRITICAL")

        # Raising a leave request FOR someone else
        if out and type_name:
            dx = base_day + timedelta(days=11)
            r = rq.post("/api/leave-requests", {"employee_id": out.emp, "leave_type_name": type_name,
                                                "from_date": dx.isoformat(), "to_date": dx.isoformat(), "days": 1,
                                                "reason": f"{TAG} on-behalf {RUN}"})
            is_hr_admin = "HR" in rq.role or "Owner" in rq.role
            ok = r.status_code in (403, 404) or is_hr_admin
            record("leave", actor, "cannot raise leave on behalf of unrelated employee"
                   + (" (HR/Owner may)" if is_hr_admin else ""), ok, r.status_code,
                   f"{rq.email} raised leave for {out.email}: {r.text[:150]}", "CRITICAL")
            if r.status_code in (200, 201):
                created.append({"flow": "leave", "path": "/api/leave-requests", "id": body_json(r).get("id"), "by": rq.email, "note": f"on behalf of {out.email}"})

    # ---------------- HR / Owner admin flows ----------------
    hr = next((c for c in clients.values() if "HR" in c.role), None)
    owner = next((c for c in clients.values() if "Owner" in c.role), None)
    if hr:
        actor = f"{hr.role} ({hr.name})"
        r = hr.post("/api/hiring-requisitions", {"requested_by": hr.emp, "designation_title": f"{TAG} QA Engineer",
                                                 "positions_count": 1, "justification": f"{TAG} {RUN}"})
        ok = r.status_code in (200, 201)
        record("hiring-requisition", actor, "create", ok, r.status_code, r.text)
        if ok:
            rid = body_json(r).get("id")
            created.append({"flow": "hiring-requisition", "path": "/api/hiring-requisitions", "id": rid, "by": hr.email})
            appr = approver_of(hr) or owner
            if appr:
                d = appr.patch(f"/api/hiring-requisitions/{rid}", {"status": "approved", "decision_notes": TAG})
                record("hiring-requisition", f"{appr.role} ({appr.name})", "approve", d.status_code == 200, d.status_code, d.text)
        ic = next((c for c in clients.values() if "IC" in c.role and c.email.lower() not in SKIP_EMAILS), None)
        if ic:
            r = hr.post("/api/salary-revision-requests", {"employee_id": ic.emp, "current_ctc": 100000,
                                                          "proposed_ctc": 100001, "reason": f"{TAG} {RUN}"})
            ok = r.status_code in (200, 201)
            record("salary-revision", actor, f"raise for {ic.name}", ok, r.status_code, r.text)
            if ok:
                rid = body_json(r).get("id")
                created.append({"flow": "salary-revision", "path": "/api/salary-revision-requests", "id": rid, "by": hr.email})
                sr = ic.patch(f"/api/salary-revision-requests/{rid}", {"status": "approved", "decision_notes": TAG})
                record("salary-revision", f"{ic.role} ({ic.name})", "subject IC cannot approve own salary revision",
                       sr.status_code in (403, 409), sr.status_code, f"IC approved own raise: {sr.text[:150]}", "CRITICAL")
                if owner and sr.status_code not in (200,):
                    d = owner.patch(f"/api/salary-revision-requests/{rid}", {"status": "rejected", "decision_notes": TAG})
                    record("salary-revision", f"{owner.role} ({owner.name})", "reject", d.status_code == 200, d.status_code, d.text)

    # Owner with no reporting manager raising leave
    if owner:
        types = as_list(owner.get("/api/leave-types"))
        if types:
            dx = date(2027, 8, 2)
            r = owner.post("/api/leave-requests", {"employee_id": owner.emp, "leave_type_name": types[0]["name"],
                                                   "from_date": dx.isoformat(), "to_date": dx.isoformat(), "days": 1,
                                                   "reason": f"{TAG} owner {RUN}"})
            record("leave", f"{owner.role} ({owner.name})", "top-of-hierarchy user can raise leave",
                   r.status_code in (200, 201), r.status_code,
                   f"Owner/CEO has no reporting manager so can never apply for leave: {r.text[:200]}", "MEDIUM")
            if r.status_code in (200, 201):
                created.append({"flow": "leave", "path": "/api/leave-requests", "id": body_json(r).get("id"), "by": owner.email})

    # ---------------- report ----------------
    fails = [r for r in results if not r["pass"]]
    out = Path(__file__).with_name("e2e_report.json")
    out.write_text(json.dumps({"run": RUN, "tenant": args.tenant, "results": results, "created": created}, indent=2, default=str))
    print(f"\n{len(results)} checks, {len(fails)} failed. {len(created)} records created (tag {TAG}, run {RUN}). Report: {out}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
