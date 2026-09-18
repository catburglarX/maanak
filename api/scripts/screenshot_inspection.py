"""Capture the inspection detail screen, the one an officer spends the most time on.

Signs in, opens the first inspection in the register, and writes:

* ``inspection-<width>.png`` for each breakpoint, full page, for layout review;
* ``check-summary.png`` and ``next-step.png``, the two parts of the screen that
  replaced the sticky action bar;
* ``inspection-review.png``, a viewport-sized shot of the findings section, which is
  the image the README embeds.

    docker run --rm --network maanak_default -v "$PWD/api:/w" -w /w \
      -e BASE_URL=http://web:8080 maanak-browser:dev python scripts/screenshot_inspection.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_URL = os.environ.get("BASE_URL", "http://web:8080")
EMAIL = os.environ.get("DEMO_EMAIL", "reviewer@example.org")
PASSWORD = os.environ.get("DEMO_PASSWORD", "Ganga-Yamuna-2026")
OUTPUT = Path(os.environ.get("SCREENSHOT_DIR", "/w/screenshots"))
WIDTHS = (1440, 820, 390)


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
        page.wait_for_url("**/app/**", timeout=30000)

        page.goto(f"{BASE_URL}/app/inspections.html", wait_until="domcontentloaded")
        page.wait_for_selector("table.register a", timeout=20000)
        href = page.locator("table.register a").first.get_attribute("href")
        print(f"opening {href}")

        for width in WIDTHS:
            page.set_viewport_size({"width": width, "height": 950})
            page.goto(f"{BASE_URL}{href}", wait_until="domcontentloaded")
            page.wait_for_selector("#findings-summary .check-summary", timeout=20000)
            page.wait_for_timeout(1400)
            full = OUTPUT / f"inspection-{width}.png"
            page.screenshot(path=str(full), full_page=True)
            print(f"wrote {full.name} ({full.stat().st_size} bytes)")

        page.set_viewport_size({"width": 1440, "height": 950})
        page.goto(f"{BASE_URL}{href}", wait_until="domcontentloaded")
        page.wait_for_selector("#findings-summary .check-summary", timeout=20000)
        page.wait_for_timeout(1200)

        summary = page.locator("#findings-summary .check-summary")
        shot = OUTPUT / "check-summary.png"
        summary.screenshot(path=str(shot))
        print(f"wrote {shot.name}")
        print("--- check summary text ---")
        print(summary.inner_text())

        if page.locator("#next-section").is_visible():
            nxt = page.locator("#next-section")
            nxt.screenshot(path=str(OUTPUT / "next-step.png"))
            print("--- next step text ---")
            print(nxt.inner_text())
        else:
            print("next-step section is hidden (no transitions available)")

        print("--- any sticky or fixed elements ---")
        sticky = page.evaluate(
            """() => Array.from(document.querySelectorAll('body *'))
                 .filter(n => ['sticky','fixed'].includes(getComputedStyle(n).position))
                 .map(n => n.tagName.toLowerCase() + '.' + (n.className || '(none)'))"""
        )
        print(sticky or "none")

        # The README image. A viewport-sized shot of the findings section keeps the file
        # small and shows the part of the screen the README is describing, rather than a
        # five-thousand-pixel full-page capture nobody can read.
        page.locator("#findings-h").scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        page.evaluate("window.scrollBy(0, -24)")
        page.wait_for_timeout(300)
        review = OUTPUT / "inspection-review.png"
        page.screenshot(path=str(review))
        print(f"wrote {review.name} ({review.stat().st_size} bytes)")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
