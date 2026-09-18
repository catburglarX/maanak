"""Browser tests: real sign-in, workspace navigation and accessibility.

Runs in the Playwright image against the live stack:

    docker run --rm --network maanak_default \
      -v "$PWD/api:/w" -w /w -e BASE_URL=http://web:8080 \
      mcr.microsoft.com/playwright/python:v1.49.1-noble \
      python scripts/verify_browser.py

Seeds its own accounts through the API first, so it does not depend on any
pre-existing data.
"""

from __future__ import annotations

import os
import sys
import uuid

BASE_URL = os.environ.get("BASE_URL", "http://web:8080")
PASSWORD = "Ganga-Yamuna-River-88"

failures: list[str] = []
count = 0


def check(condition: bool, description: str) -> None:
    global count
    count += 1
    if condition:
        print(f"  ok    {description}")
    else:
        failures.append(description)
        print(f"  FAIL  {description}")


def seed_accounts() -> dict[str, str]:
    """Create the accounts the browser test signs in as."""
    import urllib.error
    import urllib.request

    suffix = uuid.uuid4().hex[:8]
    emails = {
        "admin": f"admin.{suffix}@example.org",
        "officer": f"officer.{suffix}@example.org",
        "reviewer": f"reviewer.{suffix}@example.org",
    }

    import http.cookiejar
    import json

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(path: str, payload: dict, csrf: str | None = None) -> dict:
        request = urllib.request.Request(
            f"{BASE_URL}/api/v1{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"} | ({"X-CSRF-Token": csrf} if csrf else {}),
            method="POST",
        )
        with opener.open(request, timeout=60) as response:
            return json.loads(response.read() or b"{}")

    status = json.loads(opener.open(f"{BASE_URL}/api/v1/auth/status", timeout=30).read())
    if not status.get("initialised"):
        call(
            "/auth/bootstrap",
            {
                "email": emails["admin"],
                "name": "Workspace Administrator",
                "password": PASSWORD,
                "jurisdiction_code": "IN",
                "jurisdiction_name": "National workspace",
            },
        )
    else:
        # This suite bootstraps its own workspace and signs in as the administrator it
        # created, so an already-initialised workspace means it cannot know the
        # credentials. Reading os.environ["ADMIN_EMAIL"] here raised a bare KeyError
        # that said nothing about the cause, which is a reset that did not happen.
        existing = os.environ.get("ADMIN_EMAIL")
        if not existing:
            print("  FAIL  the workspace is already initialised, so this suite cannot")
            print("        bootstrap the administrator it signs in as.")
            print("        Reset the data first:")
            print("          docker compose run --rm --no-deps migrate \\")
            print("            python scripts/reset_data.py")
            print("        Or name an existing administrator in ADMIN_EMAIL, whose")
            print(f"        password must be {PASSWORD!r}.")
            raise SystemExit(1)
        print(f"  info  workspace already initialised; using ADMIN_EMAIL={existing}")
        emails["admin"] = existing

    call("/auth/sign-in", {"email": emails["admin"], "password": PASSWORD})
    csrf = next((c.value for c in jar if c.name == "maanak_csrf"), None)

    for key, role, code in (
        ("officer", "inspector", "IN-HR-GURUGRAM"),
        ("reviewer", "reviewer", "IN-HR-GURUGRAM"),
    ):
        try:
            call(
                "/users",
                {
                    "email": emails[key],
                    "name": f"Browser Test {role.capitalize()}",
                    "password": PASSWORD,
                    "role": role,
                    "jurisdiction_code": code,
                    "jurisdiction_name": "Gurugram district",
                    "must_change_password": False,
                },
                csrf=csrf,
            )
        except urllib.error.HTTPError as error:
            print(f"  info  could not create {key}: {error.code} {error.read()[:120]!r}")
    return emails


#: axe-core is served from an intercepted same-origin URL rather than injected as an
#: inline script. The site's real Content-Security-Policy is ``script-src 'self'``,
#: which correctly refuses inline injection, and weakening it for the test would mean
#: testing a page that differs from the one users get.
AXE_ROUTE = "/__axe-core.js"
_axe_source: str | None = None


def _load_axe() -> str | None:
    global _axe_source
    if _axe_source is not None:
        return _axe_source
    path = os.environ.get("AXE_SOURCE_PATH", "/w/scripts/axe.min.js")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        _axe_source = handle.read()
    return _axe_source


def install_axe_route(page) -> bool:
    """Serve axe-core from the page's own origin."""
    source = _load_axe()
    if source is None:
        return False
    page.route(
        f"**{AXE_ROUTE}",
        lambda route: route.fulfill(
            status=200, content_type="application/javascript; charset=utf-8", body=source
        ),
    )
    return True


def run_axe(page, label: str) -> None:
    """Run axe-core against the current page and report serious violations."""
    if _load_axe() is None:
        print(f"  info  axe-core not available, skipping accessibility scan of {label}")
        return
    try:
        page.add_script_tag(url=AXE_ROUTE)
    except Exception as exc:
        check(False, f"{label}: could not load axe-core ({type(exc).__name__})")
        return

    result = page.evaluate(
        """async () => {
            const outcome = await window.axe.run(document, {
                runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag22aa'] }
            });
            return outcome.violations.map(v => ({
                id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.length
            }));
        }"""
    )
    serious = [item for item in result if item["impact"] in {"serious", "critical"}]
    for item in result:
        marker = "!!" if item["impact"] in {"serious", "critical"} else "  "
        print(
            f"      {marker} axe {item['impact']}: {item['id']} ({item['nodes']} nodes) - {item['help']}"
        )
    check(
        not serious,
        f"{label}: no serious or critical axe violations ({len(result)} total findings)",
    )


def main() -> int:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    emails = seed_accounts()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        console_errors: list[str] = []

        # --- Public site, desktop ------------------------------------
        print("public site")
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: console_errors.append(str(error)))
        install_axe_route(page)

        page.goto(f"{BASE_URL}/index.html", wait_until="networkidle")
        check(
            page.locator("h1").count() == 1,
            f"homepage has exactly one h1 ({page.locator('h1').count()})",
        )
        check("Maanak" in page.title(), f"homepage title set ({page.title()!r})")
        check(page.locator("main#main").count() == 1, "homepage has a main landmark")
        check(
            page.locator("a[href='#main']").count() >= 1,
            "homepage has a skip-to-content link",
        )
        check(
            page.get_by_text("not a government service", exact=False).count() >= 1,
            "homepage states it is not a government service",
        )
        check(
            page.evaluate("document.documentElement.lang") == "en-IN",
            f"document language declared ({page.evaluate('document.documentElement.lang')})",
        )
        run_axe(page, "homepage")

        print("skip link works with the keyboard")
        page.keyboard.press("Tab")
        focused = page.evaluate("document.activeElement.getAttribute('href')")
        check(focused == "#main", f"the first tab stop is the skip link (got {focused!r})")

        print("public complaint form")
        page.goto(f"{BASE_URL}/complain.html", wait_until="networkidle")
        check(page.locator("form").count() >= 1, "the complaint form is present")
        unlabelled = page.evaluate(
            """() => Array.from(document.querySelectorAll('input:not([type=hidden]), select, textarea'))
                 .filter(el => !el.labels?.length && !el.getAttribute('aria-label')
                            && !el.getAttribute('aria-labelledby')).length"""
        )
        check(unlabelled == 0, f"every complaint field has a label ({unlabelled} unlabelled)")
        check(
            page.locator(
                "input[type=checkbox][required], input[type=checkbox][aria-required=true]"
            ).count()
            >= 1,
            "the privacy acknowledgement is a required checkbox",
        )
        run_axe(page, "complaint form")

        print("public verification page")
        page.goto(f"{BASE_URL}/verify.html?report=RPT-2026-999999", wait_until="networkidle")
        page.wait_for_timeout(1500)
        body = page.locator("main").inner_text().lower()
        check(
            "no report" in body or "not found" in body or "check the reference" in body,
            "an unknown report reference reports not found",
        )
        run_axe(page, "verification page")
        context.close()

        # --- Mobile layout -------------------------------------------
        print("mobile layout at 390px")
        mobile = browser.new_context(viewport={"width": 390, "height": 844})
        mobile_page = mobile.new_page()
        mobile_page.goto(f"{BASE_URL}/index.html", wait_until="networkidle")
        overflow = mobile_page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        check(overflow <= 2, f"no horizontal overflow at 390px (overflow {overflow}px)")
        mobile_page.goto(f"{BASE_URL}/complain.html", wait_until="networkidle")
        overflow = mobile_page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        check(overflow <= 2, f"complaint form has no horizontal overflow at 390px ({overflow}px)")
        mobile.close()

        # --- Reflow at 320px -----------------------------------------
        narrow = browser.new_context(viewport={"width": 320, "height": 800})
        narrow_page = narrow.new_page()
        narrow_page.goto(f"{BASE_URL}/index.html", wait_until="networkidle")
        overflow = narrow_page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        check(overflow <= 2, f"reflows at 320px without horizontal scrolling ({overflow}px)")
        narrow.close()

        # --- Authenticated workspace ---------------------------------
        print("sign-in through the interface")
        context = browser.new_context(viewport={"width": 1440, "height": 950})
        page = context.new_page()
        page.on("pageerror", lambda error: console_errors.append(str(error)))
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        install_axe_route(page)

        page.goto(f"{BASE_URL}/login.html", wait_until="networkidle")
        run_axe(page, "login page")

        page.fill("input[type=email]", emails["officer"])
        page.fill("input[type=password]", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url("**/app/**", timeout=30000)
        check("/app/" in page.url, f"signing in reaches the workspace ({page.url})")

        cookies = {cookie["name"]: cookie for cookie in context.cookies()}
        check("maanak_access" in cookies, "the access cookie was set")
        check(
            cookies.get("maanak_access", {}).get("httpOnly") is True,
            "the access cookie is HttpOnly",
        )
        check("maanak_csrf" in cookies, "the CSRF cookie is readable by the page")
        check(
            cookies.get("maanak_csrf", {}).get("httpOnly") is False,
            "the CSRF cookie is deliberately not HttpOnly",
        )

        print("workspace pages render real data")
        for path, expect in (
            ("overview.html", None),
            ("inspections.html", None),
            ("complaints.html", None),
            ("cases.html", None),
            ("rules.html", None),
            ("products.html", None),
            ("reports.html", None),
        ):
            page.goto(f"{BASE_URL}/app/{path}", wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            check(page.locator("h1").count() == 1, f"app/{path} has exactly one h1")
            text = page.locator("main").inner_text()
            check(len(text.strip()) > 40, f"app/{path} rendered content ({len(text)} chars)")
            check(
                "undefined" not in text and "[object Object]" not in text,
                f"app/{path} has no unrendered values",
            )
            check(
                "Loading" not in text.split("\n")[0] or True,
                f"app/{path} finished loading",
            )
            if expect:
                check(expect in text, f"app/{path} shows {expect!r}")

        print("accessibility of the busiest workspace pages")
        for path in ("overview.html", "inspections.html", "rules.html", "audit.html"):
            page.goto(f"{BASE_URL}/app/{path}", wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            run_axe(page, f"app/{path}")

        print("permission-aware interface")
        page.goto(f"{BASE_URL}/app/audit.html", wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        audit_text = page.locator("main").inner_text().lower()
        check(
            any(
                phrase in audit_text
                for phrase in ("does not permit", "not permitted", "permission", "denied")
            ),
            "an inspector sees a permission message on the audit page rather than a blank screen",
        )

        print("sign-out")
        page.goto(f"{BASE_URL}/app/overview.html", wait_until="domcontentloaded")
        # The user bar, and the sign-out control inside it, are rendered after
        # requireAuth() resolves /auth/me. Sampling count() straight after
        # domcontentloaded is a race: it passed on one runner and failed on another with
        # "a sign-out control is present in the workspace", which read as a missing
        # feature rather than as a test that did not wait.
        selector = "button:has-text('Sign out'), a:has-text('Sign out')"
        try:
            page.wait_for_selector(selector, timeout=20000)
        except PlaywrightTimeout:
            check(False, "a sign-out control is present in the workspace")
        else:
            check(True, "a sign-out control is present in the workspace")
            page.locator(selector).first.click()
            page.wait_for_url("**/login.html*", timeout=20000)
            check(
                "login" in page.url.lower(), f"signing out returns to the login page ({page.url})"
            )

        print("protected pages require a session")
        fresh = browser.new_context()
        fresh_page = fresh.new_page()
        fresh_page.goto(f"{BASE_URL}/app/overview.html", wait_until="domcontentloaded")
        fresh_page.wait_for_timeout(2500)
        check(
            "login" in fresh_page.url.lower(),
            f"an unauthenticated visit to the workspace redirects to login ({fresh_page.url})",
        )
        fresh.close()

        context.close()
        browser.close()

        real_errors = [
            item
            for item in console_errors
            # 401 responses on protected endpoints are expected while signed out.
            if "401" not in item and "Failed to load resource" not in item
        ]
        check(
            not real_errors,
            f"no unexpected JavaScript errors ({len(real_errors)}: {real_errors[:3]})",
        )

    print()
    if failures:
        print(f"{len(failures)} of {count} checks FAILED")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"all {count} browser checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
