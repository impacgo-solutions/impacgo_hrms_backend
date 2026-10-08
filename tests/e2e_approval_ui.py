"""Playwright browser check of the approval-mode UI in the running Flutter
web app (default http://127.0.0.1:5173) on the QA tenants from
tests/e2e_approval_setup.py. Screenshots -> build/e2e_ui/.

    cd backend && <venv>/Scripts/python -m tests.e2e_approval_ui [app_url]
"""

from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright, expect

APP = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5173"
OUT = os.path.join(os.path.dirname(__file__), "..", "..", "build", "e2e_ui")
PASSWORD = "QaTest@2026!"
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))


def shot(page, name):
    os.makedirs(OUT, exist_ok=True)
    page.screenshot(path=os.path.join(OUT, f"{name}.png"), full_page=True)


def open_app(page):
    page.goto(APP)
    page.wait_for_selector("flt-semantics-placeholder", state="attached", timeout=180_000)
    page.wait_for_timeout(1500)
    # Turn on Flutter's accessibility tree so elements are addressable.
    page.evaluate("document.querySelector('flt-semantics-placeholder')?.click()")
    page.wait_for_timeout(800)


def login(page, email, attempt=1):
    if attempt > 1:
        page.reload()
    open_app(page)
    boxes = page.get_by_role("textbox")
    expect(boxes.first).to_be_visible(timeout=60_000)
    boxes.nth(0).click()
    page.keyboard.type(email, delay=40)
    boxes.nth(1).click()
    page.keyboard.type(PASSWORD, delay=60)
    page.get_by_role("button", name="Login").click()
    # Wait until the signed-in shell is really there (Log Out is always shown,
    # in the sidebar or the narrow layout's menu).
    try:
        page.wait_for_function(
            "() => ['Log Out', 'Dashboard', 'Leave', 'Open menu'].some(t => "
            "document.body.innerText.includes(t) || "
            "document.querySelector(`flt-semantics [aria-label*=\"${t}\"]`) !== null)",
            timeout=60_000)
    except Exception:
        shot(page, "login_timeout_" + email.split("@")[0])
        if attempt < 2:  # automated typing occasionally drops a keystroke
            return login(page, email, attempt + 1)
        raise
    page.wait_for_timeout(1500)


def nav(page, label):
    page.get_by_text(label, exact=True).first.click()
    page.wait_for_timeout(2500)


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        # ── Owner of the CHAIN tenant: Administration > Approval Workflows ──
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        login(page, "owner@qa-chain-approval.example.com")
        check("Owner (chain tenant) logs in", page.get_by_text("Administration", exact=True).count() > 0)
        nav(page, "Administration")
        page.get_by_text("Approval Workflows", exact=True).first.click()
        page.wait_for_timeout(3000)
        check("Company approval mode card is shown", page.get_by_text("Company approval mode").count() > 0)
        check("Shows 'Chain approval' option", page.get_by_text("Chain approval").count() > 0)
        check("Shows 'Parallel approval' option", page.get_by_text("Parallel approval").count() > 0)
        check("Current mode reads Chain", page.get_by_text("Chain", exact=True).count() > 0)
        check("Chain steps listed (Step dropdowns)", page.get_by_text("Reporting Manager").count() > 0)
        check("Per-type editor 'Customize one request type' present",
              page.get_by_text("Customize one request type").count() > 0)
        shot(page, "1_chain_tenant_approval_workflows")
        page.close()

        # ── Owner of the PARALLEL tenant ──
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        login(page, "owner@qa-parallel-approval.example.com")
        nav(page, "Administration")
        page.get_by_text("Approval Workflows", exact=True).first.click()
        page.wait_for_timeout(3000)
        check("Parallel tenant: current mode reads Parallel", page.get_by_text("Parallel", exact=True).count() > 0)
        shot(page, "2_parallel_tenant_approval_workflows")
        page.close()

        # ── E1 (chain tenant): Leave > approval progress timeline ──
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        login(page, "e1@qa-chain-approval.example.com")
        nav(page, "Leave")
        page.wait_for_timeout(2000)
        btn = page.get_by_role("button", name="Approval progress")
        check("Leave rows show the Approval progress button", btn.count() > 0, str(btn.count()))
        if btn.count():
            btn.first.click()
            page.wait_for_timeout(2500)
            check("Progress dialog opens with step timeline",
                  page.get_by_text("Sequential approval").count() > 0
                  and page.get_by_text("Step 1", exact=False).count() > 0)
            shot(page, "3_e1_leave_approval_progress")
        page.close()

        # ── Phone width: the company card on a 390px screen ──
        page = browser.new_page(viewport={"width": 390, "height": 900})
        login(page, "owner@qa-chain-approval.example.com")
        page.get_by_role("button", name="Open menu").click()
        page.wait_for_timeout(1200)
        nav(page, "Administration")
        page.get_by_text("Approval Workflows", exact=True).first.click()
        page.wait_for_timeout(3000)
        check("Phone: company approval card renders", page.get_by_text("Company approval mode").count() > 0)
        shot(page, "4_phone_approval_workflows")
        page.close()
        browser.close()


if __name__ == "__main__":
    run()
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} UI checks passed  (screenshots: build/e2e_ui/)")
    sys.exit(0 if passed == len(results) else 1)
