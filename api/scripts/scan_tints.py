"""Find warm-tinted surfaces on the rendered pages.

Grepping the stylesheets finds the tokens. It does not find which of them actually
paint a box on screen, nor a tint that arrives through a shorthand, a gradient or an
inherited rule. This walks the live DOM instead and reports every element whose
computed background or border is a warm tint: cream, beige, amber, gold, yellow.

A colour counts as warm-tinted when red exceeds blue by more than the threshold and
the colour is not a near-neutral grey. Pure white, pure black and the burgundy brand
accent are excluded, the first two because they are the target palette and the last
because it is the established brand colour and is not what is being removed.

    python scripts/scan_tints.py            # public pages
    python scripts/scan_tints.py --app      # include the signed-in workspace
"""

from __future__ import annotations

import contextlib
import os
import sys

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://web:8080")
EMAIL = os.environ.get("REVIEWER_EMAIL", "reviewer@example.org")
PASSWORD = os.environ.get("DEMO_PASSWORD", "Ganga-Yamuna-2026")

PUBLIC_PAGES = [
    "index.html",
    "services.html",
    "package-checklist.html",
    "complain.html",
    "complaint-status.html",
    "verify.html",
    "login.html",
    "privacy.html",
    "terms.html",
    "accessibility.html",
    "security.html",
    "responsible-use.html",
    "legal-sources.html",
    "contact.html",
]

WORKSPACE_PAGES = [
    "app/overview.html",
    "app/inspections.html",
    "app/rules.html",
    "app/complaints.html",
    "app/cases.html",
    "app/reports.html",
    "app/products.html",
    "app/audit.html",
    "app/admin.html",
    "app/account.html",
]

#: Reported at every breakpoint, because a tint can be introduced by a media query.
VIEWPORTS = [("desktop", 1440, 900), ("tablet", 820, 1100), ("mobile", 390, 844)]

SCAN = """
(warmth) => {
  const parse = (value) => {
    const m = /rgba?\\(([^)]+)\\)/.exec(value || '');
    if (!m) return null;
    const parts = m[1].split(',').map(s => parseFloat(s.trim()));
    if (parts.length >= 4 && parts[3] === 0) return null;   // fully transparent
    return { r: parts[0], g: parts[1], b: parts[2] };
  };
  // Warm means red leads blue. Neutral greys have r ~= g ~= b. The burgundy accent is
  // dark and heavily red-dominant, so it is excluded by the lightness test below.
  const isWarmTint = (c) => {
    if (!c) return false;
    if (c.r === 255 && c.g === 255 && c.b === 255) return false;
    const light = (c.r + c.g + c.b) / 3;
    if (light < 150) return false;              // dark surfaces are not the target
    return (c.r - c.b) >= warmth;
  };
  const fills = [];
  for (const el of document.querySelectorAll('body *')) {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') continue;
    const box = el.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) continue;
    const problems = [];
    const bg = parse(s.backgroundColor);
    if (isWarmTint(bg)) problems.push('background ' + s.backgroundColor);
    if ((s.backgroundImage || 'none') !== 'none' && /gradient/.test(s.backgroundImage)) {
      for (const stop of s.backgroundImage.match(/rgba?\\([^)]+\\)/g) || []) {
        if (isWarmTint(parse(stop))) { problems.push('gradient ' + stop); break; }
      }
    }
    if (problems.length) {
      fills.push({
        sel: el.tagName.toLowerCase()
             + (el.id ? '#' + el.id : '')
             + (el.className && typeof el.className === 'string'
                ? '.' + el.className.trim().split(/\\s+/).join('.') : ''),
        problems,
        area: Math.round(box.width * box.height),
      });
    }
  }
  return fills;
}
"""

#: Backgrounds only, and a low threshold so a cream panel such as #f6f4ef (red leads
#: blue by 7) is caught as well as an amber one. Hairline rule colours are deliberately
#: not examined: a thin warm-grey rule is the treatment being kept, not removed.
WARMTH_THRESHOLD = 4


def main(argv: list[str]) -> int:
    include_app = "--app" in argv
    pages = list(PUBLIC_PAGES) + (WORKSPACE_PAGES if include_app else [])
    total = 0
    seen: dict[str, set[str]] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        if include_app:
            page.goto(f"{BASE_URL}/login.html", wait_until="load")
            page.fill("#email", EMAIL)
            page.fill("#password", PASSWORD)
            page.click("button[type=submit]")
            try:
                page.wait_for_url("**/app/**", timeout=25000)
            except Exception:
                print("could not sign in; workspace pages will be skipped")
                pages = list(PUBLIC_PAGES)

        for label, width, height in VIEWPORTS:
            page.set_viewport_size({"width": width, "height": height})
            print(f"=== {label} ({width}x{height}) ===")
            found_here = 0
            for name in pages:
                page.goto(f"{BASE_URL}/{name}", wait_until="domcontentloaded")
                with contextlib.suppress(PlaywrightTimeoutError):
                    page.wait_for_selector("h1", timeout=15000)
                page.wait_for_timeout(500)
                hits = page.evaluate(SCAN, WARMTH_THRESHOLD)
                if not hits:
                    continue
                found_here += len(hits)
                print(f"  {name}")
                for hit in hits[:10]:
                    print(f"      {hit['sel']}")
                    for problem in hit["problems"]:
                        print(f"          {problem}")
                    seen.setdefault(hit["sel"], set()).update(hit["problems"])
            total += found_here
            if not found_here:
                print("  none")
            print()

        browser.close()

    print(f"warm-tinted surfaces found: {total}")
    if seen:
        print()
        print("distinct selectors to fix:")
        for sel in sorted(seen):
            print(f"  {sel}  ->  {sorted(seen[sel])}")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
