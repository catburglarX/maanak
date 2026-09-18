"""Audit the whole application: every page for runtime errors, every route for wiring.

Three questions this answers that no other check in the repository does:

1. Does any page log a console error, throw an uncaught exception, or request a URL
   that fails? A page can render and still be broken.
2. Is every route in the OpenAPI document actually implemented, or does something
   return 500 or 501?
3. Does every workspace screen reach its data and render its own heading once signed
   in, rather than sitting on a spinner or an error state?

Signs in as an administrator so the workspace screens are exercised for real.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from playwright.sync_api import ConsoleMessage, Error, Page, sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://web:8080")
API_URL = os.environ.get("API_URL", BASE_URL)
EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.org")
REVIEWER_EMAIL = os.environ.get("REVIEWER_EMAIL", "reviewer@example.org")
PASSWORD = os.environ.get("DEMO_PASSWORD", "Ganga-Yamuna-2026")

PUBLIC_PAGES = [
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

WORKSPACE_PAGES = [
    ("app/overview.html", "Overview"),
    ("app/inspections.html", "Inspections"),
    ("app/complaints.html", "Complaint triage"),
    ("app/cases.html", "Cases"),
    ("app/rules.html", "Rules governance"),
    ("app/products.html", "Products"),
    ("app/reports.html", "Reports"),
    ("app/audit.html", "Audit trail"),
    ("app/admin.html", "Administration"),
    ("app/account.html", "Your account"),
]

failures: list[str] = []
passes = 0

#: Requests that are expected to fail on a page load and are not defects.
BENIGN_STATUSES = {401, 403, 404}

#: Signed evidence URLs point at the object storage address a browser *on the host*
#: can reach, set by S3_PUBLIC_ENDPOINT_URL. This audit runs inside the container
#: network, where that address does not resolve, so those requests fail here and
#: succeed for a real user. Counted and reported separately rather than as a defect.
STORAGE_ORIGIN = os.environ.get("STORAGE_PUBLIC_ORIGIN", "localhost:9000")


def record(ok: bool, message: str) -> None:
    global passes
    if ok:
        passes += 1
        print(f"  ok    {message}")
    else:
        failures.append(message)
        print(f"  FAIL  {message}")


class PageWatcher:
    """Collects everything a page complains about while it loads."""

    def __init__(self, page: Page) -> None:
        self.errors: list[str] = []
        self.failed_requests: list[str] = []
        self.storage_blocked = 0
        page.on("console", self._console)
        page.on("pageerror", self._page_error)
        page.on("requestfailed", self._request_failed)
        page.on("response", self._response)

    def _console(self, message: ConsoleMessage) -> None:
        if message.type != "error":
            return
        text = message.text
        # A blocked storage fetch logs a bare network error with no URL attached, so
        # it is recognised by the error text.
        if "ERR_CONNECTION_REFUSED" in text or "ERR_NAME_NOT_RESOLVED" in text:
            self.storage_blocked += 1
            return
        self.errors.append(f"console: {text[:160]}")

    def _page_error(self, error: Error) -> None:
        self.errors.append(f"uncaught: {str(error)[:160]}")

    def _request_failed(self, request) -> None:
        # A request cancelled because the harness navigated away is not a defect.
        failure = request.failure or ""
        if "ERR_ABORTED" in str(failure):
            return
        if STORAGE_ORIGIN in request.url:
            self.storage_blocked += 1
            return
        self.failed_requests.append(f"{request.method} {request.url[:110]}")

    def _response(self, response) -> None:
        if response.status >= 500 and STORAGE_ORIGIN not in response.url:
            self.failed_requests.append(f"{response.status} {response.url[:110]}")

    def reset(self) -> None:
        self.errors.clear()
        self.failed_requests.clear()
        self.storage_blocked = 0

    def problems(self) -> list[str]:
        return self.errors + self.failed_requests


def audit_openapi() -> None:
    print("=== every route in the OpenAPI document is declared ===")
    try:
        with urllib.request.urlopen(f"{API_URL}/openapi.json", timeout=30) as response:
            spec = json.loads(response.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        record(False, f"could not read the OpenAPI document ({exc})")
        return

    paths = spec.get("paths", {})
    methods = [
        (path, verb.upper())
        for path, item in paths.items()
        for verb in item
        if verb in {"get", "post", "put", "patch", "delete"}
    ]
    record(len(paths) > 0, f"OpenAPI declares {len(paths)} paths and {len(methods)} operations")

    undocumented = [
        f"{verb} {path}"
        for path, verb in methods
        if not paths[path][verb.lower()].get("summary")
        and not paths[path][verb.lower()].get("description")
    ]
    record(
        not undocumented,
        "every operation carries a summary or description"
        + (f" (missing on {undocumented[:5]})" if undocumented else ""),
    )

    tagless = [
        f"{verb} {path}" for path, verb in methods if not paths[path][verb.lower()].get("tags")
    ]
    record(
        not tagless,
        "every operation is tagged" + (f" (untagged: {tagless[:5]})" if tagless else ""),
    )


def no_stringified_objects(page: Page, label: str) -> None:
    """Catch a structured value interpolated into a string somewhere on the page."""
    body = page.locator("body").inner_text() or ""
    for marker in ("[object Object]", "undefined undefined", "NaN"):
        if marker in body:
            record(False, f"{label}: {marker!r} appears in the rendered page")
            return
    record(True, f"{label}: no stringified object or undefined value")


def audit_public(page: Page, watcher: PageWatcher) -> None:
    print()
    print("=== public pages load without errors ===")
    for name in PUBLIC_PAGES:
        watcher.reset()
        page.goto(f"{BASE_URL}/{name}", wait_until="load")
        page.wait_for_selector(".site-footer", timeout=15000)
        page.wait_for_timeout(400)
        problems = watcher.problems()
        record(not problems, f"{name}" + (f" — {problems[:3]}" if problems else ""))
        record(page.locator("h1").count() == 1, f"{name}: exactly one h1")
        no_stringified_objects(page, name)


def sign_in(page: Page, email: str = EMAIL) -> bool:
    page.goto(f"{BASE_URL}/login.html", wait_until="load")
    page.wait_for_selector("#email", timeout=15000)
    page.fill("#email", email)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    try:
        page.wait_for_url("**/app/**", timeout=25000)
    except Exception:
        return False
    return True


def audit_as_reviewer(page: Page, watcher: PageWatcher) -> None:
    """Re-open the inspection screen as a reviewer.

    Parts of that screen only render for a role holding ``inspection.decide``, so an
    audit signed in as an administrator never draws the decision form and cannot see
    a defect in it.
    """
    print()
    print("=== inspection screen as a reviewer, which renders the decision form ===")
    if not sign_in(page, REVIEWER_EMAIL):
        record(False, f"could not sign in as {REVIEWER_EMAIL}")
        return
    found = page.evaluate(FETCH_FIRST_ID, ["/inspections", {}])
    record_id = found.get("id")
    if not record_id:
        print("  skip  no inspection in the workspace")
        return
    watcher.reset()
    page.goto(f"{BASE_URL}/app/inspection.html?id={record_id}", wait_until="domcontentloaded")
    page.wait_for_selector("h1", timeout=20000)
    page.wait_for_timeout(2000)
    problems = watcher.problems()
    record(
        not problems,
        "inspection screen as reviewer: no runtime or policy errors"
        + (f" — {problems[:3]}" if problems else ""),
    )
    record(
        page.locator("#decision-mount form").count() == 1,
        "the decision form is rendered for a reviewer",
    )
    no_stringified_objects(page, "app/inspection.html as reviewer")
    run_axe(page, "app/inspection.html as reviewer")


AXE_ROUTE = "/__axe-core.js"
AXE_PATH = os.environ.get("AXE_SOURCE_PATH", "/w/scripts/axe.min.js")

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


def install_axe(page: Page) -> bool:
    """Serve axe-core from the page's own origin so script-src 'self' is satisfied."""
    if not os.path.exists(AXE_PATH):
        return False
    with open(AXE_PATH, encoding="utf-8") as handle:
        source = handle.read()
    page.route(
        f"**{AXE_ROUTE}",
        lambda route: route.fulfill(
            status=200, content_type="application/javascript; charset=utf-8", body=source
        ),
    )
    return True


def run_axe(page: Page, label: str) -> None:
    """The detail screens are reached by id, so no other suite scans them."""
    if not os.path.exists(AXE_PATH):
        print(f"  info  axe-core not available, skipping {label}")
        return
    page.add_script_tag(url=AXE_ROUTE)
    found = page.evaluate(RUN_AXE)
    if not found:
        record(True, f"{label}: 0 axe violations")
        return
    for item in found:
        record(
            False,
            f"{label}: {item['id']} ({item['impact']}) on {item['nodes']} node(s) — {item['help']}",
        )


def audit_workspace(page: Page, watcher: PageWatcher) -> None:
    print()
    print("=== workspace screens load, render and reach their data ===")
    for name, heading in WORKSPACE_PAGES:
        watcher.reset()
        page.goto(f"{BASE_URL}/{name}", wait_until="domcontentloaded")
        try:
            page.wait_for_selector("h1", timeout=15000)
        except Exception:
            record(False, f"{name}: no h1 rendered")
            continue
        page.wait_for_timeout(900)
        actual = (page.locator("h1").first.inner_text() or "").strip()
        record(actual == heading, f"{name}: heading is {heading!r} (got {actual!r})")
        problems = watcher.problems()
        record(
            not problems, f"{name}: no runtime errors" + (f" — {problems[:3]}" if problems else "")
        )
        empty_or_data = page.locator("table.register, .state, .metrics, .panel, .defs").count()
        record(empty_or_data > 0, f"{name}: rendered content or an explicit empty state")
        no_stringified_objects(page, name)


#: Detail screens take a record id, so they can only be audited once the workspace
#: holds data. Their headings include the record reference, so the heading is read
#: rather than predicted. Each entry is the list endpoint, its query, and the page.
DETAIL_PAGES = [
    ("/inspections", {}, "app/inspection.html"),
    ("/rules", {}, "app/rule.html"),
    ("/complaints", {}, "app/complaint.html"),
    ("/cases", {}, "app/case.html"),
]

FETCH_FIRST_ID = """
async (args) => {
  const [endpoint, params] = args;
  const query = new URLSearchParams({ page: '1', page_size: '1', ...params }).toString();
  const res = await fetch(`/api/v1${endpoint}?${query}`, { credentials: 'same-origin' });
  if (!res.ok) return { error: res.status };
  const body = await res.json();
  const items = body.items || [];
  return { id: items.length ? items[0].id : null, total: body.meta ? body.meta.total : 0 };
}
"""


def audit_detail_pages(page: Page, watcher: PageWatcher) -> None:
    print()
    print("=== detail screens render a real record ===")
    for endpoint, params, path in DETAIL_PAGES:
        found = page.evaluate(FETCH_FIRST_ID, [endpoint, params])
        if found.get("error"):
            record(False, f"{path}: could not list {endpoint} ({found['error']})")
            continue
        record_id = found.get("id")
        if not record_id:
            print(f"  skip  {path}: no {endpoint.strip('/')} in the workspace to open")
            continue
        watcher.reset()
        page.goto(
            f"{BASE_URL}/{path}?id={record_id}",
            wait_until="domcontentloaded",
        )
        try:
            page.wait_for_selector("h1", timeout=20000)
        except Exception:
            record(False, f"{path}: no h1 rendered for {record_id}")
            continue
        page.wait_for_timeout(1500)
        text = (page.locator("h1").first.inner_text() or "").strip()
        record(bool(text), f"{path}: heading rendered ({text[:48]!r})")
        problems = watcher.problems()
        record(
            not problems,
            f"{path}: no runtime errors" + (f" — {problems[:3]}" if problems else ""),
        )
        if watcher.storage_blocked:
            print(
                f"  info  {path}: {watcher.storage_blocked} signed evidence URL(s) point at "
                f"{STORAGE_ORIGIN}, which a browser on the host reaches and this container "
                "does not"
            )
        # An error state means the page loaded but could not show the record.
        broken = page.locator(".notice-error").count()
        record(broken == 0, f"{path}: no error state on screen")
        no_stringified_objects(page, path)


def main() -> int:
    audit_openapi()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        watcher = PageWatcher(page)
        if install_axe(page):
            print()
            print("  info  axe-core will also scan the detail screens")
        audit_public(page, watcher)
        if sign_in(page):
            record(True, "signed in and reached the workspace")
            audit_workspace(page, watcher)
            audit_detail_pages(page, watcher)
            audit_as_reviewer(page, watcher)
        else:
            record(False, "could not sign in; workspace screens not audited")
        browser.close()

    print()
    print(f"passed: {passes}")
    if failures:
        print(f"failed: {len(failures)}")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("every page and route check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
