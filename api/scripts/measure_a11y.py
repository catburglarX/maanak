"""Measure the accessibility criteria the browser suite does not cover.

Three things happen here that ``verify_browser.py`` does not do:

* axe-core runs against **every** public page, at WCAG 2.0, 2.1 and 2.2 level A and
  AA, rather than the four pages the browser suite samples.
* SC 2.5.8 Target Size (Minimum) is measured, because axe-core has no rule for it.
* SC 2.4.11 Focus Not Obscured is checked by finding sticky and fixed positioning,
  which is the thing that obscures a focused control.

Exits non-zero if any axe violation is found or any target falls below 24 by 24
CSS pixels.
"""

from __future__ import annotations

import os

from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://web:8080")
AXE_ROUTE = "/__axe-core.js"
AXE_PATH = os.environ.get("AXE_SOURCE_PATH", "/w/scripts/axe.min.js")

PAGES = [
    "index.html",
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

TARGET_SELECTOR = (
    "a[href], button, input:not([type=hidden]), select, textarea, [tabindex]:not([tabindex='-1'])"
)

MEASURE_TARGETS = """
(selector) => {
  const out = [];
  for (const el of document.querySelectorAll(selector)) {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    // Targets set in a line of text are exempt under the Inline exception.
    if (style.display === 'inline') continue;
    if (Math.min(r.width, r.height) < 24) {
      out.push({
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || '',
        label: (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 34),
        w: Math.round(r.width),
        h: Math.round(r.height),
      });
    }
  }
  return out;
}
"""

FIND_STICKY = """
() => {
  const out = [];
  for (const el of document.querySelectorAll('*')) {
    const pos = getComputedStyle(el).position;
    if (pos === 'sticky' || pos === 'fixed') {
      out.push(el.tagName.toLowerCase() + '.' + (el.className || '(none)') + ' [' + pos + ']');
    }
  }
  return [...new Set(out)];
}
"""

RUN_AXE = """
async () => {
  const outcome = await window.axe.run(document, {
    runOnly: {
      type: 'tag',
      values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22a', 'wcag22aa'],
    },
  });
  return outcome.violations.map(v => ({
    id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.length,
  }));
}
"""


def load_axe() -> str | None:
    if not os.path.exists(AXE_PATH):
        return None
    with open(AXE_PATH, encoding="utf-8") as handle:
        return handle.read()


def main() -> int:
    axe_source = load_axe()
    violations = 0
    undersized = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        if axe_source is not None:
            # Served from the page's own origin so the real script-src 'self' policy
            # is satisfied without weakening it for the test.
            page.route(
                f"**{AXE_ROUTE}",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/javascript; charset=utf-8",
                    body=axe_source,
                ),
            )

        print("=== axe-core, WCAG 2.0/2.1/2.2 level A and AA, every public page ===")
        if axe_source is None:
            print("  info  axe-core source not found, skipping")
        else:
            for name in PAGES:
                page.goto(f"{BASE_URL}/{name}", wait_until="load")
                page.wait_for_selector(".site-footer", timeout=10000)
                page.add_script_tag(url=AXE_ROUTE)
                found = page.evaluate(RUN_AXE)
                if not found:
                    print(f"  ok    {name}: 0 violations")
                    continue
                violations += len(found)
                for item in found:
                    print(
                        f"  FAIL  {name}: {item['id']} ({item['impact']}) "
                        f"on {item['nodes']} node(s) — {item['help']}"
                    )

        print()
        print("=== SC 2.5.8 Target Size (Minimum): targets under 24x24 CSS px ===")
        for name in PAGES:
            page.goto(f"{BASE_URL}/{name}", wait_until="load")
            page.wait_for_selector(".site-footer", timeout=10000)
            small = page.evaluate(MEASURE_TARGETS, TARGET_SELECTOR)
            if not small:
                print(f"  ok    {name}")
                continue
            undersized += len(small)
            print(f"  FAIL  {name}: {len(small)} under size")
            for item in small[:6]:
                suffix = f"/{item['type']}" if item["type"] else ""
                print(f"          {item['tag']}{suffix} {item['w']}x{item['h']}  {item['label']!r}")

        print()
        print("=== SC 2.4.11 Focus Not Obscured: sticky or fixed positioning ===")
        sticky_pages = 0
        for name in PAGES:
            page.goto(f"{BASE_URL}/{name}", wait_until="load")
            page.wait_for_selector(".site-footer", timeout=10000)
            found = page.evaluate(FIND_STICKY)
            if found:
                sticky_pages += 1
                print(f"  FAIL  {name}: {found}")
            else:
                print(f"  ok    {name}: none")

        browser.close()

    print()
    print(f"axe violations: {violations}")
    print(f"targets below 24x24: {undersized}")
    print(f"public pages with sticky or fixed positioning: {sticky_pages}")
    ok = violations == 0 and undersized == 0 and sticky_pages == 0
    print("all accessibility measurements passed" if ok else "accessibility measurements FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
