"""End-to-end test of the WorkTrack API (routers/worktrack.py) on the qa-worktrack test tenant only.

Run from backend/:  python tests/e2e_worktrack_api.py
(Create the tenant first: python -m app.provision_tenant --tenant qa-worktrack ... see app/provision_tenant.py.)"""
import datetime as dt
import os
import sys

os.environ["EMAIL_ALLOWED_DOMAINS"] = "impacgo.com"   # no real mail
os.environ["RATE_LIMIT_ENABLED"] = "false"
sys.path.insert(0, ".")
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

c = TestClient(app)
results = []
import random
RUN = str(random.randint(1000, 9999))


def check(name, cond, info=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  -- {info}" if info and not cond else ""))


def login(email, pw):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    return r.json().get("access_token") if r.status_code == 200 else None


def q(tok, **body):
    return c.post("/api/worktrack/query", json=body, headers={"Authorization": f"Bearer {tok}"})


OWNER = ("owner@qa-worktrack.example.com", "QaTest@2026!")
admin = login(*OWNER)
check("owner login", admin)
H = {"Authorization": f"Bearer {admin}"}
s = c.get("/api/worktrack/session", headers=H).json()
check("session is admin", s.get("is_admin") is True and s["user_metadata"]["role"] == "System Administrator", s)
admin_emp = s["id"]

r = q(admin, table="employees", op="select", filters=[["id", "eq", admin_emp]])
check("admin row has role System Administrator", r.json()["rows"] and r.json()["rows"][0]["role"] == "System Administrator", r.text)

# add employee
r = q(admin, table="employees", op="insert", values={"employee_id": "WT-" + RUN, "name": "Ravi Teja", "email": "ravi" + RUN + "@qa-worktrack.example.com",
                                                      "phone": "+91 9000000001", "role": "Frontend Developer", "department": "Engineering", "status": "Active"})
temp = r.json().get("temp_password")
check("add employee -> HRMS employee + temp password", r.status_code == 200 and temp, r.text)
emp_id = r.json()["rows"][0]["id"] if r.status_code == 200 else None
r = q(admin, table="employees", op="insert", values={"employee_id": "WT-" + RUN, "name": "Dup", "email": "dup@qa-worktrack.example.com", "role": "QA", "department": "Engineering"})
check("duplicate employee id rejected (23505)", r.status_code == 409 and r.json()["detail"]["code"] == "23505", r.text)

# project
due = (dt.date.today() + dt.timedelta(days=30)).isoformat()
r = q(admin, table="projects", op="insert", values={"name": "WT Portal " + RUN, "client": "Acme Retail", "company_name": "Acme Retail Pvt Ltd",
                                                     "contact": "+91 40 1234", "email": "pm@acme.example.com", "status": "In Progress",
                                                     "progress": 0.1, "due_date": due, "assigned_employee_ids": [emp_id], "attachments": []})
check("add project", r.status_code == 200 and r.json()["rows"][0]["client"] == "Acme Retail", r.text)
proj = r.json()["rows"][0]["id"]
r = q(admin, table="projects", op="select", order=[["created_at", False]])
row = next((p for p in r.json()["rows"] if p["id"] == proj), {})
check("project fields round-trip", row.get("assigned_employee_ids") == [emp_id] and row.get("company_name") == "Acme Retail Pvt Ltd"
      and row.get("due_date") == due, row)

# employee login with temp password
emp = login("ravi" + RUN + "@qa-worktrack.example.com", temp)
check("new employee logs in with temp password", emp)
se = c.get("/api/worktrack/session", headers={"Authorization": f"Bearer {emp}"}).json()
check("employee session not admin", se.get("is_admin") is False and se["id"] == emp_id, se)
r = q(emp, table="employees", op="select")
check("employee sees only own employee row", len(r.json()["rows"]) == 1, r.text)

# work entry
today = dt.date.today().isoformat()
r = q(emp, table="work_entries", op="insert", values={"user_id": emp_id, "entry_date": today, "project_id": proj, "project_name": "WT Portal " + RUN,
                                                      "description": "Built login screen", "remarks": "Pairing with QA", "start_time": "09:00:00",
                                                      "end_time": "18:00:00", "hours_worked": 6.5, "location": "Work From Home", "billable": True})
r2 = q(emp, table="work_entries", op="select", filters=[["user_id", "eq", emp_id], ["entry_date", "gte", today], ["entry_date", "lte", today]])
rows = r2.json()["rows"]
check("work entry saved with WorkTrack fields", r.status_code == 200 and rows and rows[0]["location"] == "Work From Home"
      and rows[0]["remarks"] == "Pairing with QA" and rows[0]["hours_worked"] == 6.5, r.text)
entry = rows[0]["id"] if rows else None
hr = c.get("/api/work-entries", headers={"Authorization": f"Bearer {emp}"}).json()
check("same entry visible in HRMS Work & Timesheet", any(e["id"] == entry for e in hr), str(hr)[:300])
r = q(emp, table="work_entries", op="update", values={"status": "Submitted", "submitted_at": dt.datetime.utcnow().isoformat()},
      filters=[["id", "eq", entry]])
check("employee submits entry", r.status_code == 200 and r.json()["rows"][0]["status"] == "Submitted", r.text)
r = q(emp, table="work_entries", op="update", values={"status": "Approved"}, filters=[["id", "eq", entry]])
check("employee cannot approve", r.status_code == 403, r.text)
r = q(emp, table="work_entries", op="insert", values={"user_id": emp_id, "entry_date": today, "project_id": "other",
                                                      "project_name": "Internal Tools " + RUN, "description": "x", "hours_worked": 1, "location": "Client Site", "billable": False})
check("'Other' new project via work entry", r.status_code == 200, r.text)
r = q(emp, table="work_entries", op="insert", values={"user_id": emp_id, "entry_date": (dt.date.today() + dt.timedelta(days=3)).isoformat(),
                                                      "project_id": proj, "hours_worked": 2})
check("future date rejected (HRMS rule)", r.status_code == 400, r.text)
# New HRMS rules (work_rules): several entries a day, allocation, policy
r = q(emp, table="work_entries", op="insert", values={"user_id": emp_id, "entry_date": today, "project_id": proj, "project_name": "WT Portal " + RUN,
                                                      "description": "Code review", "start_time": "09:00:00", "end_time": "18:00:00",
                                                      "hours_worked": 1.5, "location": "Work From Office", "billable": True})
check("second entry same day allowed (no fake overlap)", r.status_code == 200, r.text)
r = q(admin, table="projects", op="insert", values={"name": "Unassigned " + RUN, "client": "Acme Retail", "status": "In Progress"})
unassigned = r.json()["rows"][0]["id"]
r = q(emp, table="work_entries", op="insert", values={"user_id": emp_id, "entry_date": today, "project_id": unassigned, "hours_worked": 1,
                                                      "description": "x", "location": "Work From Office", "billable": True})
check("entry on a project you aren't allocated to is refused (HRMS M-25)", r.status_code == 403, r.text)
r = c.post("/api/worktrack/me/password", json={"new_password": "password"}, headers={"Authorization": f"Bearer {emp}"})
check("weak password refused (HRMS password policy)", r.status_code == 422, r.text)

# admin approves / rejects month
r = q(admin, table="work_entries", op="update", values={"status": "Approved"},
      filters=[["user_id", "eq", emp_id], ["entry_date", "gte", today[:8] + "01"], ["entry_date", "lte", today]])
check("admin approves month", r.status_code == 200 and all(x["status"] == "Approved" for x in r.json()["rows"]), r.text)
r = q(admin, table="work_entries", op="update", values={"status": "Rejected"}, filters=[["id", "eq", entry]])
hr = {e["id"]: e for e in c.get("/api/work-entries", headers=H).json()}
check("reject syncs HRMS entry status", r.status_code == 200 and hr.get(entry, {}).get("status") == "rejected", hr.get(entry))
r = q(admin, table="work_entries", op="select", or_groups=[[["user_id", "eq", emp_id], ["user_id", "eq", admin_emp]]])
check("admin or-filter select", r.status_code == 200 and len(r.json()["rows"]) >= 2, r.text)

# check-in -> HRMS attendance
now = dt.datetime.now()
r = q(emp, table="employee_checkins", op="insert", values={"user_id": emp_id, "employee_id": "WT-" + RUN, "employee_name": "Ravi Teja",
                                                           "employee_email": "ravi" + RUN + "@qa-worktrack.example.com", "log_type": "IN",
                                                           "time": now.strftime("%I:%M:%S %p"), "date": now.strftime("%d-%m-%Y"),
                                                           "mode_of_work": "WFH", "location_device_id": "Home"})
check("check-in saved", r.status_code == 200, r.text)
att = c.get("/api/attendance/records", headers={"Authorization": f"Bearer {emp}"}).json()
items = att.get("items", att) if isinstance(att, dict) else att
check("check-in created HRMS attendance record", any(i.get("check_in") for i in items), str(att)[:300])
r = q(emp, table="employee_checkins", op="select", filters=[["user_id", "eq", emp_id]], order=[["created_at", False]])
check("time logs list", r.status_code == 200 and r.json()["rows"][0]["log_type"] == "IN", r.text)

# activities / invoices / tasks
r = q(emp, table="activities", op="insert", values={"employee_id": "WT-" + RUN, "employee_name": "Ravi Teja", "employee_email": "ravi" + RUN + "@qa-worktrack.example.com",
                                                    "action": "Timesheet Submitted", "project": "WT Portal " + RUN, "time": "10:00:00 AM", "status": "Pending"})
check("activity logged", r.status_code == 200, r.text)
r = q(admin, table="activities", op="update", values={"status": "Approved"}, filters=[["employee_email", "eq", "ravi" + RUN + "@qa-worktrack.example.com"]])
check("admin updates activity status", r.status_code == 200 and r.json()["rows"][0]["status"] == "Approved", r.text)
r = q(admin, table="activities", op="select", order=[["created_at", False]], limit=10)
check("admin activity feed", r.status_code == 200 and len(r.json()["rows"]) >= 1, r.text)
r = q(emp, table="freelancer_invoices", op="insert", values={"user_id": emp_id, "employee_name": "Ravi Teja", "month_year": "October 2026",
                                                             "total_hours": 6.5, "hourly_rate": 1000, "currency": "INR", "total_amount": 6500,
                                                             "projects": ["WT Portal " + RUN], "status": "Submitted"})
check("freelancer invoice", r.status_code == 200, r.text)
r = q(emp, table="tasks", op="select", columns="*, projects:project_id (name)", filters=[["assigned_to", "eq", emp_id]])
check("my tasks query", r.status_code == 200, r.text)

# profile + photo-less metadata + password
r = q(emp, table="employees", op="upsert", values={"id": emp_id, "name": "Ravi Teja K", "phone": "+91 9000000002", "department": "Engineering"})
check("employee updates own profile", r.status_code == 200 and r.json()["rows"][0]["name"] == "Ravi Teja K", r.text)
r = c.put("/api/worktrack/session/metadata", json={"data": {"manager": "Qa Owner", "work_schedule": "Mon - Fri (Standard)"}}, headers=H)
check("admin profile metadata", r.status_code == 200 and r.json()["user_metadata"]["manager"] == "Qa Owner", r.text)
r = c.post("/api/worktrack/me/password", json={"new_password": "NewPass@2026"}, headers={"Authorization": f"Bearer {emp}"})
emp2 = login("ravi" + RUN + "@qa-worktrack.example.com", "NewPass@2026")
check("employee changes own password (works in HRMS login too)", r.status_code == 204 and emp2, r.text)
r = c.post("/api/worktrack/rpc/admin_reset_password", json={"target_email": "owner@qa-worktrack.example.com", "new_password": "Valid#Reset2026"},
           headers={"Authorization": f"Bearer {emp2}"})
check("employee cannot reset someone else's password", r.status_code == 403, r.text)

# delete own entry, admin deactivates employee
r = q(emp2, table="work_entries", op="delete", filters=[["id", "eq", entry]])
check("employee deletes own entry", r.status_code == 200 and not any(e["id"] == entry for e in c.get("/api/work-entries", headers=H).json()), r.text)
r = q(admin, table="employees", op="delete", filters=[["id", "eq", emp_id]])
r2 = q(admin, table="employees", op="select", filters=[["id", "eq", emp_id]])
check("admin 'delete' = inactive, data kept", r.status_code == 200 and r2.json()["rows"][0]["status"] == "Inactive", r2.text)
check("inactive employee cannot log in", login("ravi" + RUN + "@qa-worktrack.example.com", "NewPass@2026") is None)
check("inactive employee login refused with HRMS message",
      c.post("/api/auth/login", json={"email": "ravi" + RUN + "@qa-worktrack.example.com", "password": "NewPass@2026"}).status_code == 403)

# Locked week (HRMS M-24): approve the week in HRMS, WorkTrack can't edit it.
r = q(admin, table="employees", op="insert", values={"employee_id": "WL-" + RUN, "name": "Lock Test", "email": "lock" + RUN + "@qa-worktrack.example.com",
                                                      "role": "QA Engineer", "department": "Engineering"})
lock_id, lock_pw = r.json()["rows"][0]["id"], r.json()["temp_password"]
q(admin, table="projects", op="update", values={"assigned_employee_ids": [lock_id]}, filters=[["id", "eq", proj]])
lk = login("lock" + RUN + "@qa-worktrack.example.com", lock_pw)
r = q(lk, table="work_entries", op="insert", values={"user_id": lock_id, "entry_date": today, "project_id": proj, "hours_worked": 2,
                                                     "description": "Testing", "location": "Work From Office", "billable": True})
lock_entry = r.json()["rows"][0]["id"]
ts = [t for t in c.get("/api/timesheets", headers=H).json() if t["employee_id"] == lock_id]
r2 = c.patch(f"/api/timesheets/{ts[0]['id']}", json={"status": "approved"}, headers=H) if ts else None
r = q(lk, table="work_entries", op="update", values={"hours_worked": 3}, filters=[["id", "eq", lock_entry]])
check("approved (locked) week can't be edited from WorkTrack", r2 is not None and r2.status_code == 200 and r.status_code == 409,
      f"{r2.status_code if r2 is not None else 'no ts'} {r.text}")

print(f"\n{sum(ok for _, ok in results)}/{len(results)} passed")
