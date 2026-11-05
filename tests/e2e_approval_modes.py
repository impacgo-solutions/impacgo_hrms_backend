"""End-to-end (Playwright) check of Company approval mode on two real
tenants created by tests/e2e_approval_setup.py:

  qa-chain-approval     CHAIN:    R1 then R2
  qa-parallel-approval  PARALLEL: R1 or R2, first decision final

Drives the real HTTP API as each real user (Owner / E1 / R1 / R2) with
Playwright's APIRequestContext, exactly the calls the Flutter app makes,
and checks notifications, the Approvals inbox, decisions, leave status,
approval history and the email log in the database.

Run against a backend started with EMAIL_ALLOWED_DOMAINS=impacgo.com so the
test users' example.com mail is logged (SKIPPED) but never sent:

    cd backend && <venv>/Scripts/python -m tests.e2e_approval_modes [base_url]
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.sync_api import sync_playwright  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import database  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
PASSWORD = "QaTest@2026!"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))


class User:
    def __init__(self, api, slug: str, who: str):
        self.api, self.slug, self.who = api, slug, who
        r = api.post(f"{BASE}/api/auth/login", data={"email": f"{who}@{slug}.example.com", "password": PASSWORD})
        if r.status != 200:
            raise RuntimeError(f"login {who}@{slug}: {r.status} {r.text()[:200]}")
        body = r.json()
        self.token = body["access_token"]
        self.employee_id = (body.get("employee") or {}).get("id")

    def _h(self):
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, path):
        return self.api.get(f"{BASE}{path}", headers=self._h())

    def post(self, path, body):
        return self.api.post(f"{BASE}{path}", headers=self._h(), data=body)

    def put(self, path, body):
        return self.api.put(f"{BASE}{path}", headers=self._h(), data=body)

    def patch(self, path, body):
        return self.api.patch(f"{BASE}{path}", headers=self._h(), data=body)

    def notified_about(self, entity_id: str) -> bool:
        r = self.get("/api/notifications?limit=200")
        items = r.json() if r.status == 200 else []
        return any(str(n.get("entity_id")) == entity_id for n in items)

    def inbox_can_decide(self, entity_id: str):
        r = self.get("/api/approvals/inbox-sources")
        if r.status != 200:
            return None
        for item in (r.json().get("sources", {}).get("leave_requests") or []):
            if str(item.get("id")) == entity_id:
                return item.get("can_decide")
        return None


def email_recipients(slug: str, entity_id: str) -> list[tuple[str, str, str]]:
    with database.engine.connect() as c:
        rows = c.execute(text(
            f'SELECT recipient, email_type, status FROM "{slug}".core_email_logs '
            "WHERE related_entity_id = :i ORDER BY created_at"), {"i": entity_id}).all()
    return [(r[0], r[1], r[2]) for r in rows]


def apply_leave(e1: User, leave_type: str, offset: int) -> str:
    # Each run gets its own week (the app rightly refuses overlapping leave).
    run_week = int(time.time() // 300) % 60
    day = (datetime.date.today() + datetime.timedelta(days=60 + 9 * run_week + offset))
    while day.weekday() >= 5:
        day += datetime.timedelta(days=1)
    r = e1.post("/api/leave-requests", {
        "employee_id": e1.employee_id, "leave_type_name": leave_type, "from_date": day.isoformat(),
        "to_date": day.isoformat(), "days": 1, "reason": f"E2E approval test {offset}",
    })
    if r.status not in (200, 201):
        raise RuntimeError(f"apply leave: {r.status} {r.text()[:300]}")
    return str(r.json()["id"])


def decide(u: User, leave_id: str, status: str, notes: str | None = None) -> int:
    return u.patch(f"/api/leave-requests/{leave_id}", {"status": status, "decision_notes": notes}).status


def leave_status(u: User, leave_id: str) -> str | None:
    r = u.get("/api/leave-requests?limit=500")
    for row in (r.json() if r.status == 200 else []):
        if str(row.get("id")) == leave_id:
            return str(row.get("status")).lower()
    return None


def pick_leave_type(e1: User) -> str:
    r = e1.get("/api/leave-types")
    names = [t["name"] for t in r.json()] if r.status == 200 else []
    for preferred in ("Casual Leave", "Earned Leave", "Sick Leave"):
        if preferred in names:
            return preferred
    return names[0]


def run_chain(api) -> None:
    slug = "qa-chain-approval"
    print(f"\n=== {slug}  (CHAIN: R1 -> R2) ===")
    owner, e1, r1, r2 = (User(api, slug, w) for w in ("owner", "e1", "r1", "r2"))
    r = owner.put("/api/config/approval-mode", {"mode": "sequential", "steps": [
        {"approver_type": "reporting_manager"}, {"approver_type": "dotted_line_manager"}]})
    check("Owner sets Company approval mode = Chain", r.status == 200, r.text()[:200])
    mode = owner.get("/api/config/approval-mode").json()
    check("GET approval-mode returns the chain in order (R1 -> R2)",
          [s["approver_type"] for s in mode["steps"]] == ["reporting_manager", "dotted_line_manager"],
          json.dumps(mode["steps"]))
    check("GET approval-mode reports 'sequential' for every request type",
          mode["mode"] == "sequential" and {t["mode"] for t in mode["types"]} == {"sequential"}, json.dumps(mode)[:200])
    check("Non-admin (E1) cannot change the company mode",
          e1.put("/api/config/approval-mode", {"mode": "parallel", "steps": []}).status == 403)

    lt = pick_leave_type(e1)
    leave = apply_leave(e1, lt, 0)
    time.sleep(1)
    check("Submit: R1 (step 1) is notified", r1.notified_about(leave))
    check("Submit: R2 (step 2) is NOT notified yet", not r2.notified_about(leave))
    check("Inbox: R1 can decide", r1.inbox_can_decide(leave) is True, str(r1.inbox_can_decide(leave)))
    check("Inbox: R2 cannot decide yet", r2.inbox_can_decide(leave) in (False, None), str(r2.inbox_can_decide(leave)))
    check("R2 approving first is refused (403)", decide(r2, leave, "approved") == 403)
    check("Leave still pending", leave_status(e1, leave) == "pending", leave_status(e1, leave))
    check("R1 approves step 1 (200)", decide(r1, leave, "approved", "Step 1 ok") == 200)
    check("After step 1: leave still pending (waiting for R2)", leave_status(e1, leave) == "pending")
    time.sleep(1)
    check("After step 1: R2 is now notified", r2.notified_about(leave))
    check("After step 1: R2 can decide in inbox", r2.inbox_can_decide(leave) is True, str(r2.inbox_can_decide(leave)))
    check("R1 cannot approve twice (403)", decide(r1, leave, "approved") == 403)
    check("R2 approves step 2 (200)", decide(r2, leave, "approved", "Step 2 ok") == 200)
    check("Leave fully APPROVED", leave_status(e1, leave) == "approved", leave_status(e1, leave))
    check("No decision on a finished request (409)", decide(r2, leave, "rejected", "late") == 409)
    h = e1.get(f"/api/config/approval-history/leave_request/{leave}").json()
    check("History: mode sequential, both steps approved, in order",
          h.get("mode") == "sequential" and [s["state"] for s in h["steps"]] == ["approved", "approved"]
          and [s["comments"] for s in h["steps"]] == ["Step 1 ok", "Step 2 ok"], json.dumps(h)[:300])
    check("History: who + when recorded for each step", all(s["acted_by"] and s["acted_at"] for s in h["steps"]))
    mails = email_recipients(slug, leave)
    first_r1 = next((i for i, m in enumerate(mails) if m[0].startswith("r1@")), None)
    first_r2 = next((i for i, m in enumerate(mails) if m[0].startswith("r2@")), None)
    check("Email: R1 emailed before R2 (R2 only after step 1)",
          first_r1 is not None and first_r2 is not None and first_r1 < first_r2, str(mails))
    check("Email: test mail was logged but NOT sent (domain-restricted)",
          all(m[2] in ("SKIPPED",) for m in mails if "example.com" in m[0]), str(mails))

    leave2 = apply_leave(e1, lt, 3)
    check("Chain reject: R1 rejects at step 1 (200)", decide(r1, leave2, "rejected", "Busy week") == 200)
    check("Chain reject: leave REJECTED", leave_status(e1, leave2) == "rejected", leave_status(e1, leave2))
    time.sleep(1)
    check("Chain reject: R2 never notified", not r2.notified_about(leave2))
    check("Chain reject: R2 cannot act (409)", decide(r2, leave2, "approved") == 409)
    h2 = e1.get(f"/api/config/approval-history/leave_request/{leave2}").json()
    check("Chain reject: history = rejected, not reached",
          [s["state"] for s in h2["steps"]] == ["rejected", "not_reached"], json.dumps(h2)[:200])


def run_parallel(api) -> None:
    slug = "qa-parallel-approval"
    print(f"\n=== {slug}  (PARALLEL: R1 or R2) ===")
    owner, e1, r1, r2 = (User(api, slug, w) for w in ("owner", "e1", "r1", "r2"))
    r = owner.put("/api/config/approval-mode", {"mode": "parallel", "steps": [
        {"approver_type": "reporting_manager"}, {"approver_type": "dotted_line_manager"}]})
    check("Owner sets Company approval mode = Parallel", r.status == 200, r.text()[:200])
    mode = owner.get("/api/config/approval-mode").json()
    check("GET approval-mode reports 'parallel'", mode["mode"] == "parallel", json.dumps(mode)[:200])
    check("GET approval-mode returns the shared approver list (R1 or R2)",
          [s["approver_type"] for s in mode["steps"]] == ["reporting_manager", "dotted_line_manager"],
          json.dumps(mode["steps"]))

    lt = pick_leave_type(e1)
    leave = apply_leave(e1, lt, 0)
    time.sleep(1)
    check("Submit: R1 notified", r1.notified_about(leave))
    check("Submit: R2 notified at the same time", r2.notified_about(leave))
    check("Inbox: both R1 and R2 can decide",
          r1.inbox_can_decide(leave) is True and r2.inbox_can_decide(leave) is True)
    check("R2 approves first (200)", decide(r2, leave, "approved", "Fine") == 200)
    check("Leave APPROVED immediately (first decision final)", leave_status(e1, leave) == "approved",
          leave_status(e1, leave))
    check("R1 can no longer act (409)", decide(r1, leave, "approved") == 409)
    h = e1.get(f"/api/config/approval-history/leave_request/{leave}").json()
    check("History: mode parallel, approved", h.get("mode") == "parallel"
          and h["steps"] and h["steps"][0]["state"] == "approved", json.dumps(h)[:200])

    leave2 = apply_leave(e1, lt, 3)
    check("Parallel reject: R1 rejects (200)", decide(r1, leave2, "rejected", "No cover") == 200)
    check("Parallel reject: leave REJECTED", leave_status(e1, leave2) == "rejected")
    check("Parallel reject: R2 cannot act (409)", decide(r2, leave2, "approved") == 409)

    # Switch back to the standard default (empty Parallel list) and confirm.
    r = owner.put("/api/config/approval-mode", {"mode": "parallel", "steps": []})
    mode = owner.get("/api/config/approval-mode").json()
    check("Reset to default: every type uses reporting managers",
          r.status == 200 and {t["mode"] for t in mode["types"]} == {"default"}, json.dumps(mode)[:200])
    leave3 = apply_leave(e1, lt, 6)
    check("Default mode: R1 approves directly (200)", decide(r1, leave3, "approved") == 200)
    check("Default mode: leave APPROVED", leave_status(e1, leave3) == "approved")


if __name__ == "__main__":
    with sync_playwright() as p:
        api = p.request.new_context()
        try:
            run_chain(api)
            run_parallel(api)
        finally:
            api.dispose()
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    with open(os.path.join(os.path.dirname(__file__), "e2e_approval_report.json"), "w") as f:
        json.dump([{"check": n, "ok": ok, "detail": d} for n, ok, d in results], f, indent=1)
    sys.exit(0 if passed == len(results) else 1)
