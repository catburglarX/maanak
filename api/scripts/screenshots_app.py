"""Sign in through the interface with the demo credentials and capture the workspace.

    docker run --rm --network maanak_default -v "$PWD/api:/w" -w /w \
      -e BASE_URL=http://web:8080 maanak-browser:dev python scripts/screenshots_app.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_URL = os.environ.get("BASE_URL", "http://web:8080")
EMAIL = os.environ.get("DEMO_EMAIL", "reviewer@example.org")
PASSWORD = os.environ.get("DEMO_PASSWORD", "Ganga-Yamuna-2026")
OUTPUT = Path(os.environ.get("SCREENSHOT_DIR", "/w/screenshots"))

PAGES = (
    ("app-overview", "/app/overview.html"),
    ("app-inspections", "/app/inspections.html"),
    ("app-rules", "/app/rules.html"),
    ("app-complaints", "/app/complaints.html"),
)


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUTPUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 950})
        page = context.new_page()

        page.goto(f"{BASE_URL}/login.html", wait_until="domcontentloaded")
        page.fill("input[type=email]", EMAIL)
        page.fill("input[type=password]", PASSWORD)
        page.click("button[type=submit]")
        try:
            page.wait_for_url("**/app/**", timeout=30000)
        except Exception:
            print(f"sign-in did not reach the workspace; still at {page.url}")
            print(page.locator("main").inner_text()[:400])
            browser.close()
            return 1

        print(f"signed in as {EMAIL}, landed on {page.url}")

        for name, path in PAGES:
            page.goto(f"{BASE_URL}{path}", wait_until="domcontentloaded")
            page.wait_for_timeout(1600)
            destination = OUTPUT / f"{name}.png"
            page.screenshot(path=str(destination), full_page=True)
            heading = page.locator("h1").first.inner_text()
            print(f"wrote {destination.name}  h1={heading!r}  ({destination.stat().st_size} bytes)")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
