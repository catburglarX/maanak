"""Confirm the shared workspace chrome appears on every workspace screen.

The footer is mounted by layout.js rather than written into each page, because an
#app-footer div existed in one page and nothing filled it. This checks the result on
all fourteen screens, and checks that no screen serves an em dash.

    docker run --rm --network maanak_default -v "$PWD/api:/w" -w /w \
      -e BASE_URL=http://web:8080 maanak-browser:dev python scripts/check_workspace_chrome.py
"""

from __future__ import annotations

import os
import sys

BASE_URL = os.environ.get("BASE_URL", "http://web:8080")
EMAIL = os.environ.get("REVIEWER_EMAIL", "reviewer@example.org")
PASSWORD = os.environ.get("DEMO_PASSWORD", "Ganga-Yamuna-2026")
EM_DASH = "\u2014"

REGISTERS = (
    "overview.html",
    "inspections.html",
    "complaints.html",
    "cases.html",
    "rules.html",
    "products.html",
    "reports.html",
    "audit.html",
    "admin.html",
    "account.html",
)
DETAILS = ("inspection.html", "rule.html", "complaint.html", "case.html")


def main() -> int:
    from playwright.sync_api import sync_playwright

    failures = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_context(viewport={"width": 1440, "height": 950}).new_page()

        page.goto(f"{BASE_URL}/login.html", wait_until="domcontentloaded")
        page.fill("input[type=email]", EMAIL)
        page.fill("input[type=password]", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url("**/app/**", timeout=30000)

        targets = [f"/app/{name}" for name in REGISTERS]
        for name in DETAILS:
            register = {
                "inspection.html": "inspections.html",
                "rule.html": "rules.html",
                "complaint.html": "complaints.html",
                "case.html": "cases.html",
            }[name]
            page.goto(f"{BASE_URL}/app/{register}", wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            link = page.locator("table.register a").first
            if link.count() == 0:
                print(f"  info  no record in {register}, skipping {name}")
                continue
            targets.append(link.get_attribute("href"))

        for path in targets:
            page.goto(f"{BASE_URL}{path}", wait_until="domcontentloaded")
            page.wait_for_timeout(900)
            footer = page.locator("footer.app-footer")
            header = page.locator("header.app-header")
            body = page.locator("body").inner_text()
            problems = []
            if header.count() != 1:
                problems.append(f"{header.count()} headers")
            if footer.count() != 1:
                problems.append(f"{footer.count()} footers")
            elif "not a government service" not in footer.inner_text():
                problems.append("footer missing the standing note")
            if footer.count() == 1 and footer.locator("a").count() != 3:
                problems.append(f"{footer.locator('a').count()} footer links")
            if EM_DASH in body:
                problems.append("em dash in rendered text")
            if problems:
                failures += 1
                print(f"  FAIL  {path}: {', '.join(problems)}")
            else:
                print(f"  ok    {path}: one header, one footer, no em dash")

        browser.close()

    print()
    print(f"workspace chrome problems: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
