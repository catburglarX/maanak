"""Capture screenshots of the public pages for visual review.

    docker run --rm --network maanak_default -v "$PWD/api:/w" -w /w \
      -e BASE_URL=http://web:8080 maanak-browser:dev python scripts/screenshots.py

Writes PNG files to /w/screenshots, which is api/screenshots on the host.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_URL = os.environ.get("BASE_URL", "http://web:8080")
OUTPUT = Path(os.environ.get("SCREENSHOT_DIR", "/w/screenshots"))

TARGETS: tuple[tuple[str, str, int, int, bool], ...] = (
    ("home-desktop", "/index.html", 1440, 900, True),
    ("home-mobile", "/index.html", 390, 844, True),
    ("complain-desktop", "/complain.html", 1440, 900, False),
    ("verify-desktop", "/verify.html", 1440, 900, False),
    ("login-desktop", "/login.html", 1440, 900, False),
)


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUTPUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for name, path, width, height, full_page in TARGETS:
            context = browser.new_context(
                viewport={"width": width, "height": height}, device_scale_factor=1
            )
            page = context.new_page()
            page.goto(f"{BASE_URL}{path}", wait_until="networkidle")
            page.wait_for_timeout(700)
            destination = OUTPUT / f"{name}.png"
            page.screenshot(path=str(destination), full_page=full_page)
            size = destination.stat().st_size
            print(f"wrote {destination.name} ({width}x{height}, {size} bytes)")
            context.close()
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
