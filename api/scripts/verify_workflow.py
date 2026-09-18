"""Verify the governed workflow end to end.

Covers: rule seeding, simulation, maker-checker approval, activation, candidate
review, running the checks, decision gating, and cross-jurisdiction denial.

    docker compose run --rm --no-deps -e API_URL=http://api:8000 \
        --entrypoint python api scripts/verify_workflow.py
"""

from __future__ import annotations

import io
import os
import sys
import time
import uuid
from datetime import date

import httpx
from PIL import Image, ImageDraw, ImageFont

BASE_URL = os.environ.get("API_URL", "http://api:8000")
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


def csrf(client: httpx.Client) -> dict[str, str]:
    token = client.cookies.get("maanak_csrf")
    return {"X-CSRF-Token": token} if token else {}


def font(size: int, *, devanagari: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        ["/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf"]
        if devanagari
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def make_label() -> bytes:
    """A synthetic declaration panel with one deliberately wrong unit sale price.

    Clearly sample data. The unit price is printed as Rs 9.00 per 100 g while the
    declared price and quantity give Rs 5.80, so the consistency check has something
    real to find and the officer has something real to correct.
    """
    image = Image.new("RGB", (1700, 1150), (250, 249, 246))
    draw = ImageDraw.Draw(image)
    lines = [
        ("SUNRISE ATTA  (SAMPLE DATA)", 60, False),
        ("Common Name: Whole Wheat Flour", 40, False),
        ("Net Quantity: 1 kg", 46, False),
        ("शुद्ध मात्रा: 1 किलो", 42, True),
        ("M.R.P. Rs. 58.00 (incl. of all taxes)", 50, False),
        ("Unit Sale Price: Rs 9.00 per 100 g", 40, False),
        ("Mfd. by: Sunrise Foods Private Limited", 36, False),
        ("Plot 12, Industrial Area, Sample City 122001", 30, False),
        ("Consumer care: care@example.org  1800 200 1234", 30, False),
        ("Country of Origin: India", 36, False),
        ("Batch No: SA2026C     Mfg Date: 03/2026", 34, False),
    ]
    y = 36
    for text, size, deva in lines:
        draw.text((40, y), text, fill=(16, 16, 18), font=font(size, devanagari=deva))
        y += size + 24
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    admin_email = f"admin.{suffix}@example.org"
    author_email = f"ruleauthor.{suffix}@example.org"
    approver_email = f"ruleapprover.{suffix}@example.org"
    officer_email = f"officer.{suffix}@example.org"
    reviewer_email = f"reviewer.{suffix}@example.org"
    outsider_email = f"outsider.{suffix}@example.org"

    with httpx.Client(base_url=BASE_URL, timeout=180) as admin:
        print("accounts")
        response = admin.post(
            "/api/v1/auth/bootstrap",
            json={
                "email": admin_email,
                "name": "Workspace Administrator",
                "password": PASSWORD,
                "jurisdiction_code": "IN",
                "jurisdiction_name": "National workspace",
            },
        )
        check(response.status_code == 201, f"administrator created ({response.status_code})")
        admin.post("/api/v1/auth/sign-in", json={"email": admin_email, "password": PASSWORD})

        def make_account(email: str, role: str, code: str, name: str) -> dict:
            created = admin.post(
                "/api/v1/users",
                headers=csrf(admin),
                json={
                    "email": email,
                    "name": name,
                    "password": PASSWORD,
                    "role": role,
                    "jurisdiction_code": code,
                    "jurisdiction_name": code,
                    "must_change_password": False,
                },
            )
            check(
                created.status_code == 201,
                f"{role} {name} created ({created.status_code}"
                + (f": {created.text[:200]}" if created.status_code != 201 else "")
                + ")",
            )
            return created.json() if created.status_code == 201 else {}

        make_account(author_email, "rule_admin", "IN", "Rule Author")
        make_account(approver_email, "rule_admin", "IN", "Rule Approver")
        make_account(officer_email, "inspector", "IN-HR-GURUGRAM", "District Inspector")
        make_account(reviewer_email, "reviewer", "IN-HR-GURUGRAM", "District Reviewer")
        make_account(outsider_email, "inspector", "IN-PB-LUDHIANA", "Other State Inspector")

    # ------------------------------------------------------------------
    # Rule governance
    # ------------------------------------------------------------------
    with httpx.Client(base_url=BASE_URL, timeout=180) as author:
        author.post("/api/v1/auth/sign-in", json={"email": author_email, "password": PASSWORD})
        print("rule seeding")
        seeded = author.post("/api/v1/rules/seed", headers=csrf(author))
        check(seeded.status_code == 200, f"starter rules seeded ({seeded.status_code})")
        payload = seeded.json()
        check(len(payload["created"]) >= 8, f"{len(payload['created'])} rule versions created")
        check(payload["status"] == "draft", f"seeded as drafts ({payload['status']})")

        listing = author.get("/api/v1/rules", params={"page_size": 100}).json()
        rules = {item["code"]: item for item in listing["items"]}
        check(
            all(item["status"] == "draft" for item in rules.values()),
            "no seeded rule is effective before approval",
        )
        check(
            all(not item["legal_authority_confirmed"] for item in rules.values()),
            "no seeded rule claims statutory confirmation",
        )
        check(
            all(not item["effective_for_inspections"] for item in rules.values()),
            "no seeded rule can influence an inspection yet",
        )

        target_code = "LMPC-UNIT-SALE-PRICE-CONSISTENCY"
        rule = rules[target_code]
        rule_id = rule["id"]

        detail = author.get(f"/api/v1/rules/{rule_id}").json()
        check(
            "not been verified" in (detail["uncertainty_note"] or "").lower(),
            "the seeded citation is explicitly marked unverified",
        )
        check(
            bool(detail["governance"]["approval_blockers"]),
            f"approval blockers listed ({detail['governance']['approval_blockers']})",
        )

        print("approval is refused before simulation")
        submitted = author.post(
            f"/api/v1/rules/{rule_id}/status",
            headers=csrf(author),
            json={
                "target_status": "under_review",
                "expected_version": detail["record_version"],
            },
        )
        check(submitted.status_code == 200, f"submitted for review ({submitted.status_code})")
        detail = submitted.json()

        # Checked with the approver account, not the author: otherwise the
        # separation-of-duties rule fires first and the simulation gate is never
        # reached. Both gates are exercised, one at a time.
        with httpx.Client(base_url=BASE_URL, timeout=60) as early_approver:
            early_approver.post(
                "/api/v1/auth/sign-in", json={"email": approver_email, "password": PASSWORD}
            )
            premature = early_approver.post(
                f"/api/v1/rules/{rule_id}/status",
                headers=csrf(early_approver),
                json={"target_status": "approved", "expected_version": detail["record_version"]},
            )
        check(
            premature.status_code == 409,
            f"approval refused without a simulator run ({premature.status_code})",
        )
        if premature.status_code == 409:
            check(
                premature.json()["error"]["code"] == "simulation_required",
                f"stable error code ({premature.json()['error']['code']})",
            )

        print("simulation")
        simulated = author.post(
            f"/api/v1/rules/{rule_id}/simulate",
            headers=csrf(author),
            json={"scenarios": [], "use_seeded_scenarios": True, "include_generated": True},
        )
        check(simulated.status_code == 200, f"simulation ran ({simulated.status_code})")
        report = simulated.json()
        check(report["all_passed"], f"every scenario passed ({report['failed_count']} failed)")
        check(
            not report["missing_kinds"],
            f"every required case covered (missing: {report['missing_kinds']})",
        )
        check(report["approval_ready"], "simulation reports approval ready")
        check(len(report["scenarios"]) >= 4, f"{len(report['scenarios'])} scenarios run")

        print("separation of duties")
        detail = author.get(f"/api/v1/rules/{rule_id}").json()
        self_approve = author.post(
            f"/api/v1/rules/{rule_id}/status",
            headers=csrf(author),
            json={"target_status": "approved", "expected_version": detail["record_version"]},
        )
        check(
            self_approve.status_code == 403,
            f"the author cannot approve their own version ({self_approve.status_code})",
        )
        if self_approve.status_code == 403:
            check(
                self_approve.json()["error"]["code"] == "self_approval_refused",
                "stable self-approval error code",
            )

    with httpx.Client(base_url=BASE_URL, timeout=180) as approver:
        approver.post("/api/v1/auth/sign-in", json={"email": approver_email, "password": PASSWORD})
        detail = approver.get(f"/api/v1/rules/{rule_id}").json()
        approved = approver.post(
            f"/api/v1/rules/{rule_id}/status",
            headers=csrf(approver),
            json={
                "target_status": "approved",
                "note": "Interpretation and arithmetic reviewed against the definition.",
                "expected_version": detail["record_version"],
            },
        )
        check(
            approved.status_code == 200,
            f"a second rule administrator approved it ({approved.status_code})",
        )
        detail = approved.json()
        check(detail["status"] == "approved", f"status is approved ({detail['status']})")
        check(
            detail["effective_for_inspections"], "an approved version can influence an inspection"
        )
        check(
            not detail["legal_authority_confirmed"],
            "workspace approval does not imply statutory confirmation",
        )
        check(
            any(item["decision"] == "approved" for item in detail["reviews"]),
            "the approval is recorded in the review history",
        )

        # Approve and activate the remaining seeded rules so the inspection has a
        # full rule set to run against.
        listing = approver.get("/api/v1/rules", params={"page_size": 100}).json()
        activated = 0
        for item in listing["items"]:
            if item["status"] == "approved":
                continue
            current = approver.get(f"/api/v1/rules/{item['id']}").json()
            if current["status"] == "draft":
                current = approver.post(
                    f"/api/v1/rules/{item['id']}/status",
                    headers=csrf(approver),
                    json={
                        "target_status": "under_review",
                        "expected_version": current["record_version"],
                    },
                ).json()
            approver.post(
                f"/api/v1/rules/{item['id']}/simulate",
                headers=csrf(approver),
                json={"scenarios": [], "use_seeded_scenarios": True, "include_generated": True},
            )
            current = approver.get(f"/api/v1/rules/{item['id']}").json()
            result = approver.post(
                f"/api/v1/rules/{item['id']}/status",
                headers=csrf(approver),
                json={
                    "target_status": "approved",
                    "note": "Reviewed as part of the starter set.",
                    "expected_version": current["record_version"],
                },
            )
            if result.status_code == 200:
                activated += 1
        check(activated >= 7, f"{activated} further rule versions approved")

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------
    with httpx.Client(base_url=BASE_URL, timeout=300) as officer:
        officer.post("/api/v1/auth/sign-in", json={"email": officer_email, "password": PASSWORD})
        headers = csrf(officer)

        print("inspection and evidence")
        created = officer.post(
            "/api/v1/inspections",
            headers=headers,
            json={
                "inspection_date": date.today().isoformat(),
                "source": "retail_store",
                "premises_name": "Sample Retail Store",
                "product": {
                    "brand": "Sunrise",
                    "name": "Atta 1 kg pouch (sample data)",
                    "common_generic_name": "Whole wheat flour",
                    "commodity_category": "cereals_and_pulses",
                    "is_food": True,
                    "package_type": "pouch",
                    "quantity_kind": "weight",
                    "declared_net_quantity": "1",
                    "declared_net_quantity_unit": "kg",
                },
            },
        )
        check(created.status_code == 201, f"inspection opened ({created.status_code})")
        inspection = created.json()
        inspection_id = inspection["id"]

        upload = officer.post(
            f"/api/v1/inspections/{inspection_id}/evidence",
            headers=headers,
            data={"face": "declaration_panel"},
            files={"file": ("panel.jpg", make_label(), "image/jpeg")},
        )
        check(upload.status_code == 202, f"evidence accepted ({upload.status_code})")
        job_id = upload.json()["job_id"]

        for _ in range(90):
            time.sleep(2)
            job = officer.get(f"/api/v1/jobs/{job_id}").json()
            if job["state"] in {"succeeded", "failed"}:
                break
        check(job["state"] == "succeeded", f"analysis finished ({job['state']})")

        detail = officer.get(f"/api/v1/inspections/{inspection_id}").json()
        candidates = detail["candidates"]
        check(len(candidates) >= 6, f"{len(candidates)} readings produced")

        print("running checks before review produces no conclusion from unreviewed readings")
        early = officer.post(f"/api/v1/inspections/{inspection_id}/checks", headers=headers)
        check(early.status_code == 200, f"checks ran ({early.status_code})")
        early_body = early.json()
        check(
            early_body["unreviewed_candidates"] > 0,
            f"unreviewed readings reported ({early_body['unreviewed_candidates']})",
        )
        check(
            early_body["outcomes"].get("non_compliant", 0) == 0,
            f"no violation is asserted from unreviewed readings ({early_body['outcomes']})",
        )

        print("candidate review, including a correction")
        corrected_one = False
        for candidate in candidates:
            current = officer.get(f"/api/v1/inspections/{inspection_id}").json()
            live = next(
                (item for item in current["candidates"] if item["id"] == candidate["id"]), None
            )
            if live is None or live["review_state"] != "pending":
                continue

            if live["declaration_type"] == "unit_sale_price" and not corrected_one:
                # The printed unit price is wrong on this package. The officer records
                # the printed value verbatim, which is what the rule must test.
                response = officer.patch(
                    f"/api/v1/candidates/{live['id']}",
                    headers=headers,
                    json={
                        "review_state": "corrected",
                        "corrected_text": "Rs 9.00",
                        "correction_reason": "Confirmed the printed figure against the photograph.",
                        "expected_version": live["version"],
                    },
                )
                check(response.status_code == 200, f"correction accepted ({response.status_code})")
                if response.status_code == 200:
                    body = response.json()
                    check(
                        body["review_state"] == "corrected",
                        f"review state recorded as corrected ({body['review_state']})",
                    )
                    check(
                        body["normalised_value"].get("amount") is not None,
                        "the machine reading is preserved alongside the correction",
                    )
                    check(
                        body["corrected_value"].get("amount") == "9.00",
                        f"corrected value parsed ({body['corrected_value'].get('amount')})",
                    )
                    history = officer.get(f"/api/v1/candidates/{live['id']}/history").json()
                    check(len(history) >= 1, f"a revision was written ({len(history)})")
                    check(
                        history[0]["previous_state"]["review_state"] == "pending",
                        "the revision records the previous state",
                    )
                corrected_one = True
                continue

            officer.patch(
                f"/api/v1/candidates/{live['id']}",
                headers=headers,
                json={
                    "review_state": "confirmed",
                    "review_note": "Matches the photograph.",
                    "expected_version": live["version"],
                },
            )

        detail = officer.get(f"/api/v1/inspections/{inspection_id}").json()
        check(
            detail["pending_candidate_count"] == 0,
            f"every reading reviewed ({detail['pending_candidate_count']} pending)",
        )
        check(corrected_one, "one reading was corrected rather than confirmed")

        print("stale write is refused")
        first = detail["candidates"][0]
        stale = officer.patch(
            f"/api/v1/candidates/{first['id']}",
            headers=headers,
            json={
                "review_state": "confirmed",
                "review_note": "Attempting a stale write.",
                "expected_version": 1,
            },
        )
        check(stale.status_code == 409, f"stale candidate write refused ({stale.status_code})")

        print("checks after review")
        checked = officer.post(f"/api/v1/inspections/{inspection_id}/checks", headers=headers)
        check(checked.status_code == 200, f"checks ran ({checked.status_code})")
        result = checked.json()
        check(result["unreviewed_candidates"] == 0, "no unreviewed readings remain")
        check(result["rules_applied"] >= 6, f"{result['rules_applied']} rules applied")
        check(result["findings_written"] >= 6, f"{result['findings_written']} findings written")
        check(
            result["outcomes"].get("non_compliant", 0) >= 1,
            f"the wrong unit price was found ({result['outcomes']})",
        )

        detail = officer.get(f"/api/v1/inspections/{inspection_id}").json()
        findings = {item["declaration_type"]: item for item in detail["findings"]}
        unit_price_finding = findings.get("unit_sale_price")
        check(unit_price_finding is not None, "a finding exists for the unit sale price")
        if unit_price_finding:
            check(
                unit_price_finding["outcome"] == "non_compliant",
                f"outcome is non-compliant ({unit_price_finding['outcome']})",
            )
            check(
                bool(unit_price_finding["calculation"]),
                f"the arithmetic is recorded ({len(unit_price_finding['calculation'])} steps)",
            )
            check(
                unit_price_finding["rule"].get("citation") is not None,
                "the finding cites a rule version",
            )
            check(
                unit_price_finding["rule"].get("legal_authority_confirmed") is False,
                "the finding shows the citation is not statutorily confirmed",
            )
            check(
                "5.80" in " ".join(unit_price_finding["calculation"]),
                "the expected unit price appears in the calculation",
            )
            check(
                bool(unit_price_finding["selection_reason"].get("reasons")),
                "why this rule version applied is recorded",
            )

        print("every finding cites an approved rule version")
        check(
            all(item["rule"].get("id") for item in detail["findings"]),
            "no finding lacks a rule version",
        )
        check(
            all(
                item["rule"].get("status") in {"approved", "active"} for item in detail["findings"]
            ),
            "every cited rule version is approved or active",
        )

        print("an inspector cannot record the decision")
        denied = officer.post(
            f"/api/v1/inspections/{inspection_id}/decision",
            headers=headers,
            json={
                "decision": "violation_found",
                "note": "An inspector should not be able to record this decision at all.",
                "expected_version": detail["version"],
            },
        )
        check(denied.status_code == 403, f"inspector denied the decision ({denied.status_code})")

        advanced = officer.post(
            f"/api/v1/inspections/{inspection_id}/transitions",
            headers=headers,
            json={"target_state": "reviewer_review", "expected_version": detail["version"]},
        )
        check(advanced.status_code == 200, f"sent for reviewer decision ({advanced.status_code})")

    print("cross-jurisdiction denial")
    with httpx.Client(base_url=BASE_URL, timeout=60) as outsider:
        outsider.post("/api/v1/auth/sign-in", json={"email": outsider_email, "password": PASSWORD})
        response = outsider.get(f"/api/v1/inspections/{inspection_id}")
        check(
            response.status_code == 404,
            f"an officer in another state gets 404, not 403 ({response.status_code})",
        )
        register = outsider.get("/api/v1/inspections").json()
        check(
            register["meta"]["total"] == 0,
            f"the register excludes out-of-jurisdiction records ({register['meta']['total']})",
        )

    print("reviewer decision")
    with httpx.Client(base_url=BASE_URL, timeout=120) as reviewer:
        reviewer.post("/api/v1/auth/sign-in", json={"email": reviewer_email, "password": PASSWORD})
        headers = csrf(reviewer)
        detail = reviewer.get(f"/api/v1/inspections/{inspection_id}").json()

        short = reviewer.post(
            f"/api/v1/inspections/{inspection_id}/decision",
            headers=headers,
            json={
                "decision": "violation_found",
                "note": "Too short.",
                "expected_version": detail["version"],
            },
        )
        check(short.status_code == 422, f"a token reason is refused ({short.status_code})")

        decided = reviewer.post(
            f"/api/v1/inspections/{inspection_id}/decision",
            headers=headers,
            json={
                "decision": "violation_found",
                "note": (
                    "The printed unit sale price of Rs 9.00 per 100 g does not agree with "
                    "the declared retail sale price of Rs 58.00 and net quantity of 1 kg, "
                    "which give Rs 5.80 per 100 g."
                ),
                "expected_version": detail["version"],
            },
        )
        check(
            decided.status_code == 200,
            f"decision recorded ({decided.status_code}: {decided.text[:160]})",
        )
        if decided.status_code == 200:
            body = decided.json()
            check(
                body["state"] == "violation_found", f"state reflects the decision ({body['state']})"
            )
            check(body["decision_note"] is not None, "the reasoned note is stored")
            check(body["decided_at"] is not None, "the decision time is stored")
            check(
                any(item["kind"] == "state_change" for item in body["timeline"]),
                "the timeline records the change",
            )
            check(
                len(body["timeline"]) >= 3,
                f"the timeline covers the whole life of the inspection ({len(body['timeline'])} entries)",
            )

        # --------------------------------------------------------------
        # Reports
        # --------------------------------------------------------------
        print("report issue")
        issued = reviewer.post(f"/api/v1/reports/inspections/{inspection_id}", headers=headers)
        check(
            issued.status_code == 201, f"report issued ({issued.status_code}: {issued.text[:200]})"
        )
        if issued.status_code != 201:
            return 1
        report = issued.json()
        report_id = report["id"]

        check(
            report["reference"].startswith("RPT-"),
            f"report reference allocated ({report['reference']})",
        )
        check(len(report["snapshot_sha256"]) == 64, "the snapshot is hashed with SHA-256")
        check(
            len(report["verification_code"]) == 14,
            f"verification code printed ({report['verification_code']})",
        )
        formats = {item["format"] for item in report["documents"]}
        check(formats == {"pdf", "docx"}, f"both formats produced ({sorted(formats)})")
        for document in report["documents"]:
            check(
                len(document["sha256"]) == 64 and document["size_bytes"] > 2000,
                f"{document['format']} stored with its own hash "
                f"({document['size_bytes']} bytes)",
            )
        pdf_meta = next(item for item in report["documents"] if item["format"] == "pdf")
        check(
            (pdf_meta.get("page_count") or 0) >= 2,
            f"the PDF has real page numbering ({pdf_meta.get('page_count')} pages)",
        )

        print("a second report is refused while one is issued")
        duplicate = reviewer.post(f"/api/v1/reports/inspections/{inspection_id}", headers=headers)
        check(duplicate.status_code == 409, f"duplicate issue refused ({duplicate.status_code})")

        print("snapshot integrity")
        snapshot = reviewer.get(f"/api/v1/reports/{report_id}/snapshot").json()
        check(snapshot["integrity"]["intact"], "the stored snapshot matches its recorded hash")
        check(
            snapshot["integrity"]["recomputed_sha256"] == report["snapshot_sha256"],
            "recomputing the hash reproduces the recorded value",
        )
        frozen = snapshot["snapshot"]
        check(frozen["inspection"]["decision"] == "violation_found", "the decision is frozen")
        check(len(frozen["evidence"]) >= 1, "evidence hashes are frozen into the snapshot")
        check(len(frozen["findings"]) >= 6, f"{len(frozen['findings'])} findings frozen")
        check(bool(frozen["rule_versions"]), "cited rule versions are frozen with their citations")
        check(
            all(
                item.get("legal_authority_confirmed") is False
                for item in frozen["rule_versions"].values()
            ),
            "the frozen rule versions record that citations are unconfirmed",
        )
        check(bool(frozen["limits"]), "the report states its own limits")
        check(
            any("not verified by weighing" in limit for limit in frozen["limits"]),
            "the report says net quantity was not physically verified",
        )
        readings = {item["declaration_type"]: item for item in frozen["readings"]}
        corrected_reading = readings.get("unit_sale_price")
        check(
            corrected_reading is not None
            and corrected_reading["machine_reading"]
            and corrected_reading["officer_corrected_value"],
            "the snapshot keeps both the machine reading and the officer correction",
        )

        print("document download")
        import hashlib

        for fmt in ("pdf", "docx"):
            response = reviewer.get(f"/api/v1/reports/{report_id}/document.{fmt}")
            check(response.status_code == 200, f"{fmt} downloaded ({response.status_code})")
            expected = next(item["sha256"] for item in report["documents"] if item["format"] == fmt)
            check(
                hashlib.sha256(response.content).hexdigest() == expected,
                f"the downloaded {fmt} matches its recorded hash",
            )
            check(
                response.headers.get("X-Document-SHA256") == expected,
                f"the {fmt} response states its hash",
            )
            if fmt == "pdf":
                check(response.content.startswith(b"%PDF-"), "the PDF is a real PDF file")
                # Page content streams are compressed, so the printed hash is not
                # findable as raw bytes. The document metadata is not compressed and
                # carries the report reference.
                check(
                    report["reference"].encode() in response.content,
                    "the PDF metadata identifies the report reference",
                )
                check(b"/Lang" in response.content, "the PDF declares a document language")
            else:
                check(response.content[:2] == b"PK", "the DOCX is a real Office Open XML file")

        print("the inspection is now frozen")
        detail = reviewer.get(f"/api/v1/inspections/{inspection_id}").json()
        check(detail["state"] == "report_issued", f"state is report_issued ({detail['state']})")
        frozen_attempt = reviewer.post(
            f"/api/v1/inspections/{inspection_id}/faces",
            headers=headers,
            json={
                "face": "back_panel",
                "capture_state": "absent",
                "reason": "Attempting a change after issue.",
            },
        )
        check(
            frozen_attempt.status_code == 409,
            f"changes are refused after a report is issued ({frozen_attempt.status_code})",
        )

    print("public verification")
    with httpx.Client(base_url=BASE_URL, timeout=60) as public:
        found = public.get(
            "/api/v1/public/reports/verify", params={"reference": report["reference"]}
        )
        check(found.status_code == 200, f"verification endpoint answers ({found.status_code})")
        body = found.json()
        check(body["found"], "the report is found by its reference")
        check(body["content_intact"], "verification confirms the content is unchanged")
        check(body["document_hash"] == report["snapshot_sha256"], "the hash shown matches")
        check(body["state"] == "issued", f"state reported ({body['state']})")
        check(
            "not a government" in body["not_a_government_service"].lower(),
            "the disclaimer is present",
        )

        # The public page must not leak the case.
        text = found.text.lower()
        for secret in (
            "sunrise",
            "premises",
            "violation_found",
            "care@example.org",
            officer_email.lower(),
        ):
            check(secret not in text, f"the public page does not reveal {secret!r}")

        by_code = public.get(
            "/api/v1/public/reports/verify", params={"code": report["verification_code"]}
        ).json()
        check(by_code.get("found") is True, "the report is found by its verification code")

        mismatch = public.get(
            "/api/v1/public/reports/verify",
            params={"reference": report["reference"], "code": "AAAA-AAAA-AAAA"},
        ).json()
        check(mismatch.get("found") is False, "a wrong verification code is rejected")

        unknown = public.get(
            "/api/v1/public/reports/verify", params={"reference": "RPT-2026-999999"}
        ).json()
        check(unknown.get("found") is False, "an unknown reference reports not found")
        check(
            "check the reference" in unknown.get("detail", "").lower(),
            "the not-found message does not confirm which part was wrong",
        )

    print()
    if failures:
        print(f"{len(failures)} of {count} checks FAILED")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"all {count} workflow checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
