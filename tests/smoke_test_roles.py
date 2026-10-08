"""End-to-end, read-only smoke test across every role in every tenant.

What it does
  1. Reads public.users + each tenant's core_users/core_user_roles/core_roles
     straight from DATABASE_URL (.env) and picks one active user per
     (tenant, role) -- the account being logged in is chosen from the DB, not
     hard-coded.
  2. Logs each one in through POST /api/auth/login (every seeded user shares
     the same password, read from the SMOKE_PASSWORD environment variable).
  3. With that token, calls every GET endpoint in /openapi.json:
       - no-parameter endpoints as-is
       - endpoints needing from_date/to_date/entity/etc. with sensible values
       - /{id}-style detail endpoints with an id taken from the matching list
         endpoint's response (or the user's own employee id)
  4. Runs negative auth checks (no token, bad token, wrong password).

Pass/fail
  FAIL  login error, any 5xx, a timeout/connection error, or a negative auth
        check that is let through.
  OK    2xx. 401/403/404/422 are recorded as "denied"/"client" -- expected
        for roles without the permission -- and summarised per role so RBAC
        can be eyeballed, but they do not fail the run.

Nothing is written except public.users.last_login_at (stamped by login).

Usage (API must be running, e.g. `uvicorn app.main:app --port 8000`):
    cd backend
    venv\\Scripts\\python tests\\smoke_test_roles.py [--base-url URL] [--tenant SLUG] [--per-role N]
Exit code is 1 when anything FAILs. A JSON report is written next to this
file as smoke_report.json.
"""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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
TIMEOUT = 60

today = date.today()
QUERY_DEFAULTS = {
    "from_date": (today - timedelta(days=30)).isoformat(),
    "to_date": today.isoformat(),
    "week_start": (today - timedelta(days=today.weekday())).isoformat(),
    "entity": "employee",
    "month": str(today.month),
    "year": str(today.year),
}


def discover_users(per_role: int, only_tenant: str | None) -> list[dict]:
    engine = create_engine(settings.database_url)
    users: list[dict] = []
    with engine.connect() as conn:
        slugs = conn.execute(
            text("SELECT DISTINCT tenant_slug FROM public.users WHERE is_active")
        ).scalars().all()
        for slug in slugs:
            if only_tenant and slug != only_tenant:
                continue
            rows = conn.execute(
                text(
                    f"""
                    SELECT role_name, email FROM (
                      SELECT r.name AS role_name, pu.email,
                             row_number() OVER (PARTITION BY r.name ORDER BY pu.email) AS rn
                      FROM public.users pu
                      JOIN "{slug}".core_users cu
                        ON cu.employee_id = coalesce(pu.employee_id, pu.id)
                      JOIN "{slug}".core_user_roles ur ON ur.user_id = cu.id
                      JOIN "{slug}".core_roles r ON r.id = ur.role_id
                      WHERE pu.tenant_slug = :slug AND pu.is_active
                        AND cu.status = 'active'
                    ) t WHERE rn <= :n ORDER BY role_name, email
                    """
                ),
                {"slug": slug, "n": per_role},
            ).all()
            if not rows:
                users.append({"tenant": slug, "role": None, "email": None})
            for role_name, email in rows:
                users.append({"tenant": slug, "role": role_name, "email": email})
    return users


def load_get_endpoints(base: str) -> list[dict]:
    spec = requests.get(f"{base}/openapi.json", timeout=TIMEOUT).json()
    endpoints = []
    for path, methods in spec["paths"].items():
        op = methods.get("get")
        if not op:
            continue
        required_query = [
            p["name"]
            for p in op.get("parameters", [])
            if p.get("in") == "query" and p.get("required")
        ]
        path_params = re.findall(r"{(\w+)}", path)
        endpoints.append({"path": path, "query": required_query, "path_params": path_params})
    return endpoints


def first_id(payload) -> str | None:
    if isinstance(payload, dict):
        for key in ("items", "data", "results", "rows"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0].get("id")
    return None


def classify(status: int | None) -> str:
    if status is None or status >= 500:
        return "FAIL"
    if 200 <= status < 300:
        return "ok"
    if status in (401, 403):
        return "denied"
    return "client"


def run_user(base: str, user: dict, endpoints: list[dict]) -> dict:
    result = {**user, "login": None, "calls": []}
    if user["email"] is None:
        result["login"] = "no users with a role in this tenant"
        return result

    s = requests.Session()
    try:
        r = s.post(
            f"{base}/api/auth/login",
            json={"email": user["email"], "password": PASSWORD},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        result["login"] = f"FAIL {exc.__class__.__name__}"
        return result
    if r.status_code != 200:
        result["login"] = f"FAIL {r.status_code} {r.text[:200]}"
        return result
    body = r.json()
    result["login"] = "ok"
    result["login_role"] = body.get("role")
    result["permissions"] = len(body.get("permissions") or [])
    own_employee_id = (body.get("employee") or {}).get("id")
    s.headers["Authorization"] = f"Bearer {body['access_token']}"

    list_ids: dict[str, str] = {}

    def call(path: str, params: dict) -> None:
        t0 = time.perf_counter()
        try:
            resp = s.get(f"{base}{path}", params=params, timeout=TIMEOUT)
            status, detail = resp.status_code, ""
            if status == 200 and "{" not in path:
                try:
                    fid = first_id(resp.json())
                    if fid:
                        list_ids[path] = str(fid)
                except ValueError:
                    pass
            if status >= 400:
                detail = resp.text[:200]
        except requests.RequestException as exc:
            status, detail = None, exc.__class__.__name__
        result["calls"].append(
            {
                "path": path,
                "status": status,
                "verdict": classify(status),
                "ms": int((time.perf_counter() - t0) * 1000),
                "detail": detail,
            }
        )

    # Pass 1: collection endpoints (also harvests ids for pass 2).
    for ep in endpoints:
        if ep["path_params"]:
            continue
        params = {q: QUERY_DEFAULTS.get(q, own_employee_id or "") for q in ep["query"]}
        if "employee_id" in ep["query"]:
            params["employee_id"] = own_employee_id
        call(ep["path"], params)

    # Pass 2: detail endpoints with a single id we can resolve.
    for ep in endpoints:
        if len(ep["path_params"]) != 1:
            continue
        parent = ep["path"].split("/{")[0]
        tail = ep["path"].split("}", 1)[1]
        if parent in list_ids:
            ident = list_ids[parent]
        elif parent == "/api/employees" and own_employee_id:
            ident = own_employee_id
        else:
            continue
        # Only simple sub-resources: /x/{id} or /x/{id}/y (skip file downloads/pdf renders).
        if tail.count("/") > 1 or any(k in tail for k in ("pdf", "download", "file", "html")):
            continue
        path = ep["path"].replace("{" + ep["path_params"][0] + "}", ident)
        params = {q: QUERY_DEFAULTS.get(q, "") for q in ep["query"]}
        call(path, params)

    return result


def negative_checks(base: str, sample_email: str | None) -> list[dict]:
    checks = []

    def check(name: str, fn, expect: tuple[int, ...]) -> None:
        try:
            status = fn().status_code
        except requests.RequestException as exc:
            status = exc.__class__.__name__
        checks.append({"check": name, "status": status, "pass": status in expect})

    check("no token -> /api/employees", lambda: requests.get(f"{base}/api/employees", timeout=TIMEOUT), (401, 403))
    check(
        "garbage token -> /api/employees",
        lambda: requests.get(
            f"{base}/api/employees", headers={"Authorization": "Bearer not.a.jwt"}, timeout=TIMEOUT
        ),
        (401, 403),
    )
    check(
        "unknown email login",
        lambda: requests.post(
            f"{base}/api/auth/login",
            json={"email": "nobody-smoke@example.invalid", "password": PASSWORD},
            timeout=TIMEOUT,
        ),
        (401,),
    )
    if sample_email:
        check(
            "wrong password login",
            lambda: requests.post(
                f"{base}/api/auth/login",
                json={"email": sample_email, "password": PASSWORD + "x"},
                timeout=TIMEOUT,
            ),
            (401,),
        )
    return checks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("SMOKE_BASE_URL", "http://localhost:8000"))
    ap.add_argument("--tenant", default="impacgo-solutions", help="tenant slug to test")
    ap.add_argument("--per-role", type=int, default=1, help="users to test per role")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    users = discover_users(args.per_role, args.tenant)
    endpoints = load_get_endpoints(base)
    print(f"{len(users)} role accounts, {len(endpoints)} GET endpoints, base={base}\n")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda u: run_user(base, u, endpoints), users))

    neg = negative_checks(base, next((u["email"] for u in users if u["email"]), None))

    failures = 0
    for res in sorted(results, key=lambda r: (r["tenant"], r["role"] or "")):
        label = f"[{res['tenant']}] {res['role']} <{res['email']}>"
        if res["login"] != "ok":
            is_fail = str(res["login"]).startswith("FAIL")
            failures += is_fail
            print(f"{'FAIL' if is_fail else 'SKIP'} {label}: login {res['login']}")
            continue
        counts: dict[str, int] = {}
        for c in res["calls"]:
            counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
        bad = [c for c in res["calls"] if c["verdict"] == "FAIL"]
        failures += len(bad)
        print(
            f"{'FAIL' if bad else 'PASS'} {label} perms={res['permissions']} "
            + " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )
        for c in bad:
            print(f"     {c['status']} GET {c['path']} {c['detail'][:150]}")

    print("\nNegative auth checks:")
    for c in neg:
        failures += not c["pass"]
        print(f"  {'PASS' if c['pass'] else 'FAIL'} {c['check']} -> {c['status']}")

    out = Path(__file__).with_name("smoke_report.json")
    out.write_text(json.dumps({"results": results, "negative": neg}, indent=2, default=str))
    print(f"\n{'FAILED' if failures else 'ALL PASSED'}: {failures} failure(s). Report: {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
