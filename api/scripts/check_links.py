"""Check that every link on every public page goes somewhere real.

Two failure modes matter and both are easy to introduce by hand:

* an internal href pointing at a page that does not exist
* a table-of-contents anchor pointing at an id that is not on the page

Both are checked against the rendered DOM, so links written by the shared chrome are
covered as well as links written into the markup.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://web:8080")

PAGES = [
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

COLLECT = """
() => {
  const anchors = [...document.querySelectorAll('a[href]')];
  return anchors.map(a => a.getAttribute('href'));
}
"""


def head_ok(url: str) -> bool:
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status == 200
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return False


def main() -> int:
    failures = 0
    checked_pages: set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        print("=== in-page anchors resolve to an element on the same page ===")
        for name in PAGES:
            page.goto(f"{BASE_URL}/{name}", wait_until="load")
            page.wait_for_selector(".site-footer", timeout=10000)
            hrefs = [h for h in page.evaluate(COLLECT) if h]

            fragments = [h[1:] for h in hrefs if h.startswith("#") and len(h) > 1]
            missing = [f for f in fragments if page.locator(f"#{f}").count() == 0]
            if missing:
                failures += len(missing)
                print(f"  FAIL  {name}: no element for {missing}")
            else:
                count = len(fragments)
                print(f"  ok    {name}: {count} anchor{'' if count == 1 else 's'}")

            for href in hrefs:
                if href.startswith("/"):
                    checked_pages.add(href.split("#")[0].lstrip("/") or "index.html")

        # Cross-page fragments, e.g. /index.html#platform used by the shared header.
        print()
        print("=== cross-page fragments resolve on their target page ===")
        cross: set[tuple[str, str]] = set()
        for name in PAGES:
            page.goto(f"{BASE_URL}/{name}", wait_until="load")
            page.wait_for_selector(".site-footer", timeout=10000)
            for href in page.evaluate(COLLECT):
                if href and href.startswith("/") and "#" in href:
                    target, _, fragment = href.partition("#")
                    if fragment:
                        cross.add((target.lstrip("/") or "index.html", fragment))
        for target, fragment in sorted(cross):
            page.goto(f"{BASE_URL}/{target}", wait_until="load")
            page.wait_for_selector(".site-footer", timeout=10000)
            found = page.locator(f"#{fragment}").count() > 0
            if not found:
                failures += 1
            print(f"  {'ok  ' if found else 'FAIL'} /{target}#{fragment}")

        browser.close()

    print()
    print("=== internal pages return 200 ===")
    for target in sorted(checked_pages):
        ok = head_ok(f"{BASE_URL}/{target}")
        if not ok:
            failures += 1
        print(f"  {'ok  ' if ok else 'FAIL'} /{target}")

    print()
    print(f"broken links and anchors: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
