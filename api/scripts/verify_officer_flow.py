"""Drive the officer workflow through the interface, the way a judge would.

Creates an inspection through the New inspection dialog, uploads a package image,
waits for the background worker to finish, and reports what the OCR produced and what
the officer can see. This exercises the parts of the pipeline that only a real browser
session reaches: the dialog, the upload form, the job poller and the raw OCR panel.

    docker run --rm --network maanak_default -v "$PWD/api:/w" -w /w \
      -e BASE_URL=http://web:8080 maanak-browser:dev python scripts/verify_officer_flow.py
"""

from __future__ import annotations

import os
import sys

BASE_URL = os.environ.get("BASE_URL", "http://web:8080")
EMAIL = os.environ.get("INSPECTOR_EMAIL", "inspector@example.org")
REVIEWER = os.environ.get("REVIEWER_EMAIL", "reviewer@example.org")
PASSWORD = os.environ.get("DEMO_PASSWORD", "Ganga-Yamuna-2026")
#: A photograph with strong glare, which the quality gate should refuse.
GLARED = os.environ.get("GLARED_IMAGE", "/repo/web/samples/compliant.jpg")
#: A clean, readable declaration panel. Written by scripts/make_sample_label.py, run in
#: the api image, because the label generator needs the application dependencies and the
#: browser image does not carry them.
CLEAN = os.environ.get("CLEAN_IMAGE", "/repo/api/screenshots/clean-label.jpg")

passed = 0
failed: list[str] = []


def check(condition: bool, label: str) -> None:
    global passed
    if condition:
        passed += 1
        print(f"  ok    {label}")
    else:
        failed.append(label)
        print(f"  FAIL  {label}")


def main() -> int:
    from playwright.sync_api import sync_playwright

    if not os.path.exists(CLEAN):
        print(f"  FAIL  no readable sample at {CLEAN}.")
        print("        Generate it first, in the api image:")
        print('          docker compose run --rm --no-deps -v "$PWD/api:/app" \\')
        print("            --entrypoint python api scripts/make_sample_label.py")
        return 1
    print(f"using the readable label at {CLEAN}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_context(viewport={"width": 1440, "height": 950}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        print("sign in as the inspecting officer")
        page.goto(f"{BASE_URL}/login.html", wait_until="domcontentloaded")
        page.fill("input[type=email]", EMAIL)
        page.fill("input[type=password]", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url("**/app/**", timeout=30000)
        check(True, f"signed in as {EMAIL}")

        print("open an inspection through the New inspection dialog")
        page.goto(f"{BASE_URL}/app/inspections.html", wait_until="domcontentloaded")
        page.wait_for_selector("#new-inspection:not([hidden])", timeout=20000)
        page.click("#new-inspection")
        page.wait_for_selector("#create-inspection", timeout=10000)
        check(page.locator("#ci-product").count() == 1, "the dialog offers a product choice")
        check(
            page.locator("#ci-new-product").is_visible(),
            "new-product fields are shown when no existing product is chosen",
        )
        options = page.locator("#ci-product option").count()
        check(options >= 2, f"existing products are listed for reuse ({options - 1} on file)")

        page.fill("#ci-brand", "Testbrand")
        page.fill("#ci-name", "Test Iodised Salt 1 kg (browser check)")
        page.fill("#ci-cat", "salt")
        page.fill("#ci-prem", "Browser Check Store")
        page.select_option("#ci-source", "retail_store")
        page.click("dialog .dlg-foot button:has-text('Create')")
        page.wait_for_url("**/app/inspection.html?id=*", timeout=30000)
        check(True, f"the inspection opened: {page.url.split('id=')[-1][:8]}")

        reference = page.locator("#context-body h1").inner_text()
        check(reference.startswith("Inspection INSP-"), f"reference allocated: {reference}")

        print("a poor image is refused with an instruction, not silently accepted")
        page.wait_for_selector("#upload-form:not([hidden])", timeout=20000)
        check(True, "the evidence upload panel is visible to an inspector")
        page.select_option("#up-face", "principal_display_panel")
        page.set_input_files("#up-file", GLARED)
        page.click("#up-btn")
        page.wait_for_selector("#upload-status .notice", timeout=60000)
        refusal = page.locator("#upload-status").inner_text().strip()
        check("not good enough" in refusal.lower(), f"refused: {refusal[:78]}")
        signals = page.locator("#up-quality").inner_text()
        check(
            "glare" in signals.lower() or "glare" in refusal.lower(),
            "the refusal names the specific problem",
        )
        check(
            page.locator("#q-override").count() == 1,
            "an override is offered, so the officer can proceed with a recorded reason",
        )

        print("upload a readable package image and let the worker read it")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#upload-form:not([hidden])", timeout=20000)
        page.select_option("#up-face", "principal_display_panel")
        page.set_input_files("#up-file", CLEAN)
        page.click("#up-btn")
        page.wait_for_selector("#upload-status .notice", timeout=60000)
        status_text = page.locator("#upload-status").inner_text().strip()
        check(
            "queued" in status_text.lower() or "uploaded" in status_text.lower(),
            f"accepted: {status_text[:78]}",
        )

        print("wait for the analysis job to finish")
        done = False
        for _ in range(60):
            page.wait_for_timeout(2000)
            jobs = page.locator("#jobs").inner_text()
            if "Analysis complete" in jobs:
                done = True
                break
            if "Analysis failed" in jobs:
                check(False, f"analysis failed: {jobs.strip()[:120]}")
                break
        check(done, "the worker reported the analysis complete")

        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#candidates", timeout=20000)
        page.wait_for_timeout(2500)

        candidates = page.locator(".candidate").count()
        check(candidates > 0, f"OCR produced {candidates} declaration candidates for review")
        if candidates:
            # The two state labels are uppercased by CSS, so compare case-insensitively.
            first = page.locator(".candidate").first.inner_text().lower()
            check("machine observed" in first, "each candidate shows the machine state")
            check(
                "officer review" in first,
                "each candidate shows the officer review state separately",
            )

        print("the raw OCR panel is reachable from the viewer")
        check(
            page.locator("#toggle-ocr").is_visible(),
            "a Raw OCR control is present on the viewer toolbar",
        )
        # Select the evidence explicitly first. The panel reads whichever face is
        # selected, and immediately after a reload the thumbnails may still be mounting.
        page.wait_for_selector("#thumbs button", timeout=20000)
        page.locator("#thumbs button").first.click()
        page.wait_for_timeout(600)
        page.click("#toggle-ocr")
        page.wait_for_timeout(3000)
        ocr = page.locator("#ocr-raw").inner_text()
        check(len(ocr.strip()) > 0, "the raw OCR panel returns text")
        check("Engine:" in ocr, "the OCR panel names the engine and languages")
        words = [w for w in ocr.split() if w.isalpha()]
        check(len(words) > 15, f"the OCR text is substantive ({len(words)} alphabetic tokens)")
        print("    ---- the OCR output the officer sees ----")
        for line in ocr.strip().splitlines()[:9]:
            print(f"    {line[:110]}")

        print("review the readings, run the checks and record a decision")
        confirmed = 0
        for index in range(page.locator(".candidate").count()):
            card = page.locator(".candidate").nth(index)
            button = card.locator("button:has-text('Confirm')")
            if button.count():
                button.first.click()
                page.wait_for_timeout(700)
                confirmed += 1
                # The page reloads its data after each review, so re-read the list.
                if confirmed >= 11:
                    break
        check(confirmed > 0, f"confirmed {confirmed} readings through the interface")

        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#findings-h", timeout=20000)
        page.wait_for_timeout(1500)
        pending = page.locator("#context-body").inner_text()
        check("Readings awaiting review" in pending, "the context panel reports the review backlog")

        if page.locator("#run-checks:not([hidden])").count():
            page.click("#run-checks")
            page.wait_for_timeout(4000)
            page.wait_for_selector("#findings-summary .check-summary", timeout=25000)
            summary = page.locator("#findings-summary").inner_text()
            check(
                "finding" in summary.lower(),
                f"checks produced findings: {summary.splitlines()[0][:70]}",
            )
            check(
                "suggested outcome" in summary.lower(),
                "the engine offers a suggested outcome without deciding",
            )
        else:
            check(False, "an inspector can run the checks")

        print("the original file and the inspection details are reachable")
        download = page.locator("#download-original")
        check(download.is_visible(), "the original evidence file can be downloaded from the viewer")
        href = download.get_attribute("href") or ""
        check("/download" in href, f"the download points at the original bytes: {href[-28:]}")

        edit = page.locator("#context-body button:has-text('Edit details')")
        check(edit.count() == 1, "inspection details can be corrected while the record is open")
        edit.click()
        page.wait_for_selector("#ed-prem", timeout=10000)
        page.fill("#ed-batch", "BROWSER-CHECK-1")
        page.click("dialog .dlg-foot button:has-text('Save')")
        page.wait_for_timeout(3000)
        check(
            "BROWSER-CHECK-1" in page.locator("main").inner_text(),
            "the correction is saved and shown on the record",
        )

        inspection_url = page.url
        check(
            errors == [],
            f"no uncaught JavaScript errors as the inspector ({len(errors)}: {errors[:2]})",
        )

        print()
        print("continue as the reviewer: decide, issue the report, open the case")
        page.goto(f"{BASE_URL}/login.html", wait_until="domcontentloaded")
        page.evaluate(
            "() => document.cookie.split(';').forEach(c => document.cookie = c.split('=')[0] + '=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/')"
        )
        page.goto(f"{BASE_URL}/login.html", wait_until="domcontentloaded")
        page.fill("input[type=email]", REVIEWER)
        page.fill("input[type=password]", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url("**/app/**", timeout=30000)
        check(True, f"signed in as {REVIEWER}")

        errors.clear()
        page.goto(inspection_url, wait_until="domcontentloaded")
        page.wait_for_selector("#next-section:not([hidden])", timeout=25000)
        page.wait_for_timeout(1200)

        # A decision is only valid from reviewer_review, so the screen should say so
        # rather than offering a form that cannot succeed.
        guidance = page.locator("#decision-mount").inner_text()
        check(
            "reviewer decision" in guidance.lower(),
            f"the decision section explains the prerequisite: {guidance.strip()[:80]}",
        )
        check(
            page.locator("#dec-outcome").count() == 0,
            "no decision form is offered before the inspection reaches that stage",
        )

        send = page.locator("#transitions button:has-text('Send for reviewer decision')")
        check(send.count() == 1, "Send for reviewer decision is offered under Next step")
        send.click()
        page.wait_for_timeout(3500)

        page.wait_for_selector("#dec-outcome", timeout=25000)
        options = [o.inner_text() for o in page.locator("#dec-outcome option").all()]
        check(
            options
            == ["Select a decision…", "Compliant", "Violation found", "Unable to determine"],
            f"the decision select offers only what the endpoint accepts: {options[1:]}",
        )

        page.select_option("#dec-outcome", "violation_found")
        page.fill(
            "#dec-note",
            "The printed unit sale price does not agree with the declared retail sale "
            "price and net quantity. Recorded during the browser verification run on "
            "sample data.",
        )
        page.click("#decision-mount button[type=submit]")
        page.wait_for_timeout(3500)
        badges = page.locator("#context-body .btn-row").first.inner_text()
        check(
            "Violation found" in badges,
            f"the decision is recorded and labelled: {badges.replace(chr(10), ' ')[:64]}",
        )
        check("violation_found" not in badges, "no raw enum value is shown to the officer")

        print("issue the report from the inspection screen")
        page.wait_for_selector("#report-section:not([hidden])", timeout=20000)
        check(
            page.locator("#report-mount button:has-text('Issue report')").count() == 1,
            "an Issue report control is offered once a decision exists",
        )
        page.click("#report-mount button:has-text('Issue report')")
        page.wait_for_selector("dialog[open]", timeout=10000)
        page.click("dialog .dlg-foot button:has-text('Issue report')")
        page.wait_for_timeout(6000)
        report_text = page.locator("#report-mount").inner_text()
        check(
            "RPT-" in report_text,
            f"a report reference was allocated: {[w for w in report_text.split() if w.startswith('RPT-')][:1]}",
        )
        check("Open the PDF" in report_text, "the PDF is linked from the inspection screen")
        check("Open the document" in report_text, "the editable document is linked")
        check("Public verification page" in report_text, "the public verification page is linked")

        print("open a case from the issued report")
        check(
            page.locator("#report-mount button:has-text('Open a case')").count() == 1,
            "an Open a case control is offered once a report exists",
        )
        page.click("#report-mount button:has-text('Open a case')")
        page.wait_for_selector("#oc-name", timeout=10000)
        page.fill("#oc-name", "Riverside Foods Private Limited")
        page.select_option("#oc-role", "manufacturer")
        page.click("dialog .dlg-foot button:has-text('Open case')")
        page.wait_for_url("**/app/case.html?id=*", timeout=30000)
        page.wait_for_timeout(2500)
        case_text = page.locator("main").inner_text()
        check("CASE-" in case_text, "the case screen opened on a real case record")
        check("Next step" in case_text, "the case screen offers its available transitions")

        check(
            errors == [],
            f"no uncaught JavaScript errors as the reviewer ({len(errors)}: {errors[:2]})",
        )
        browser.close()

    print()
    print(f"passed: {passed}")
    if failed:
        print(f"failed: {len(failed)}")
        for item in failed:
            print(f"  - {item}")
        return 1
    print("the officer workflow runs end to end through the interface")
    return 0


if __name__ == "__main__":
    sys.exit(main())
