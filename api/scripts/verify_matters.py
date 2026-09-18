"""Verify the complaint, case and audit chain end to end.

One connected story: a consumer complains, an officer triages and opens an
inspection carrying the consumer's photograph across, the readings are reviewed, the
checks run, a decision is recorded, a report is issued, a case is opened, a notice is
served and answered, and the audit chain is verified.

Finishes with a tamper test that forges an audit row and confirms the chain
verification detects it.

    docker compose run --rm --no-deps -e API_URL=http://api:8000 \
        --entrypoint python api scripts/verify_matters.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import uuid
from datetime import UTC, date, datetime, timedelta

import httpx
from PIL import Image, ImageDraw, ImageFont
from session_client import RefreshingClient

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


def font(size: int) -> ImageFont.FreeTypeFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def make_label() -> bytes:
    """Synthetic panel with a deliberately wrong unit sale price. Sample data."""
    image = Image.new("RGB", (1700, 1100), (250, 249, 246))
    draw = ImageDraw.Draw(image)
    lines = [
        ("RIVERSIDE SALT  (SAMPLE DATA)", 58),
        ("Common Name: Iodised Salt", 40),
        ("Net Quantity: 1 kg", 46),
        ("M.R.P. Rs. 24.00 (incl. of all taxes)", 50),
        ("Unit Sale Price: Rs 5.00 per 100 g", 40),
        ("Mfd. by: Riverside Foods Private Limited", 36),
        ("Plot 8, Industrial Estate, Sample City 122001", 30),
        ("Consumer care: care@example.org  1800 300 4567", 30),
        ("Country of Origin: India", 36),
        ("Batch No: RS2026A     Mfg Date: 02/2026", 34),
    ]
    y = 36
    for text, size in lines:
        draw.text((40, y), text, fill=(16, 16, 18), font=font(size))
        y += size + 26
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    admin_email = f"admin.{suffix}@example.org"
    rule_author = f"ruleauthor.{suffix}@example.org"
    rule_approver = f"ruleapprover.{suffix}@example.org"
    officer_email = f"officer.{suffix}@example.org"
    controller_email = f"controller.{suffix}@example.org"
    consumer_email = f"consumer.{suffix}@example.org"

    # ------------------------------------------------------------------
    # A consumer complains. No account, no sign-in.
    # ------------------------------------------------------------------
    print("public complaint")
    with RefreshingClient(base_url=BASE_URL, timeout=120) as public:
        submission = {
            "product_name": "Riverside Iodised Salt 1 kg (sample data)",
            "brand": "Riverside",
            "category": "mrp_overcharge",
            "description": (
                "The shop charged me 30 rupees but the packet shows 24 rupees as the "
                "maximum retail price. I have attached a photograph of the packet."
            ),
            "purchase_date": date.today().isoformat(),
            "seller_name": "Sample Retail Store",
            "stated_mrp": "24.00",
            "stated_price_paid": "30.00",
            "location_text": "Sample City",
            "contact_email": consumer_email,
            "consent_to_contact": True,
            "privacy_notice_acknowledged": True,
        }
        response = public.post(
            "/api/v1/public/complaints",
            data={"complaint": json.dumps(submission)},
            files={"package_images": ("packet.jpg", make_label(), "image/jpeg")},
        )
        check(
            response.status_code == 201,
            f"complaint accepted ({response.status_code}: {response.text[:200]})",
        )
        if response.status_code != 201:
            return 1
        complaint = response.json()
        reference = complaint["reference"]
        check(reference.startswith("CMP-"), f"reference issued ({reference})")
        check(
            complaint["priority"] == 70, f"MRP overcharge priority is 70 ({complaint['priority']})"
        )
        check(bool(complaint["priority_reason"]), "the priority reason is published")
        check(complaint["attachments_stored"] == 1, "the photograph was stored")
        check(complaint["status_lookup_available"], "status lookup is available")

        print("privacy notice is mandatory")
        refused = public.post(
            "/api/v1/public/complaints",
            data={"complaint": json.dumps({**submission, "privacy_notice_acknowledged": False})},
        )
        check(
            refused.status_code == 422,
            f"submission without acknowledgement refused ({refused.status_code})",
        )

        print("public status lookup")
        found = public.get(
            "/api/v1/public/complaints/status",
            params={"reference": reference, "contact": consumer_email},
        ).json()
        check(found.get("found") is True, "the consumer can see progress with reference plus email")
        check(found["state"] == "received", f"state shown ({found['state']})")
        check("resolution_note" in found, "the response shape is stable")

        wrong = public.get(
            "/api/v1/public/complaints/status",
            params={"reference": reference, "contact": "someone.else@example.org"},
        ).json()
        check(wrong.get("found") is False, "a wrong contact detail reveals nothing")
        guessed = public.get(
            "/api/v1/public/complaints/status",
            params={"reference": "CMP-2026-000001", "contact": "guess@example.org"},
        ).json()
        check(guessed.get("found") is False, "a guessed reference alone reveals nothing")

    # ------------------------------------------------------------------
    # Officers
    # ------------------------------------------------------------------
    with RefreshingClient(base_url=BASE_URL, timeout=120) as admin:
        admin.post(
            "/api/v1/auth/bootstrap",
            json={
                "email": admin_email,
                "name": "Workspace Administrator",
                "password": PASSWORD,
                "jurisdiction_code": "IN",
                "jurisdiction_name": "National workspace",
            },
        )
        admin.post("/api/v1/auth/sign-in", json={"email": admin_email, "password": PASSWORD})
        for email, role, code, name in (
            (rule_author, "rule_admin", "IN", "Rule Author"),
            (rule_approver, "rule_admin", "IN", "Rule Approver"),
            (officer_email, "inspector", "IN-HR-GURUGRAM", "District Inspector"),
            (controller_email, "controller", "IN-HR", "State Controller"),
        ):
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
            check(created.status_code == 201, f"{name} created ({created.status_code})")

    print("rule set approved")
    with RefreshingClient(base_url=BASE_URL, timeout=180) as author:
        author.post("/api/v1/auth/sign-in", json={"email": rule_author, "password": PASSWORD})
        seeded = author.post("/api/v1/rules/seed", headers=csrf(author))
        check(seeded.status_code == 200, f"rules seeded ({seeded.status_code})")

    with RefreshingClient(base_url=BASE_URL, timeout=300) as approver:
        approver.post("/api/v1/auth/sign-in", json={"email": rule_approver, "password": PASSWORD})
        listing = approver.get("/api/v1/rules", params={"page_size": 100}).json()
        approved = 0
        for item in listing["items"]:
            current = approver.get(f"/api/v1/rules/{item['id']}").json()
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
                approved += 1
        check(approved >= 10, f"{approved} rule versions approved")

    # ------------------------------------------------------------------
    # Complaint becomes an inspection
    # ------------------------------------------------------------------
    print("complaint handling")
    with RefreshingClient(base_url=BASE_URL, timeout=300) as officer:
        officer.post("/api/v1/auth/sign-in", json={"email": officer_email, "password": PASSWORD})
        headers = csrf(officer)

        queue = officer.get("/api/v1/complaints", params={"unassigned": True}).json()
        check(
            queue["meta"]["total"] >= 1, f"the complaint is in the queue ({queue['meta']['total']})"
        )
        entry = next(item for item in queue["items"] if item["reference"] == reference)
        complaint_id = entry["id"]
        check(entry["priority"] == 70, "the queue is ordered by published priority")

        detail = officer.get(f"/api/v1/complaints/{complaint_id}").json()
        check(detail["contact_email"] == consumer_email, "an officer can see the contact detail")
        check(len(detail["attachments"]) == 1, "the consumer photograph is attached")
        check(len(detail["attachments"][0]["sha256"]) == 64, "the attachment is hashed")

        triaged = officer.post(
            f"/api/v1/complaints/{complaint_id}/triage",
            headers=headers,
            json={
                "jurisdiction_code": "IN-HR-GURUGRAM",
                "jurisdiction_name": "Gurugram district",
                "triage_note": "Genuine complaint; the packet is a standard retail pack.",
                "expected_version": detail["version"],
            },
        )
        check(triaged.status_code == 200, f"triage recorded ({triaged.status_code})")

        detail = officer.get(f"/api/v1/complaints/{complaint_id}").json()
        converted = officer.post(
            f"/api/v1/complaints/{complaint_id}/inspection",
            headers=headers,
            json={
                "inspection_date": date.today().isoformat(),
                "premises_name": "Sample Retail Store",
                "commodity_category": "salt",
                "promote_attachments": True,
                "attachment_face": "principal_display_panel",
                "expected_version": detail["version"],
            },
        )
        check(
            converted.status_code == 201,
            f"inspection opened from the complaint ({converted.status_code}: {converted.text[:200]})",
        )
        if converted.status_code != 201:
            return 1
        inspection = converted.json()
        inspection_id = inspection["id"]
        check(
            len(inspection["evidence"]) >= 1,
            f"the consumer photograph became evidence ({len(inspection['evidence'])})",
        )
        check(
            inspection["evidence"][0]["quality_override_reason"] is not None,
            "accepting the consumer's image quality is recorded with a reason",
        )
        check(
            inspection["context"].get("opened_from_complaint") == reference,
            "the inspection records which complaint it came from",
        )

        after = officer.get(f"/api/v1/complaints/{complaint_id}").json()
        check(
            after["state"] == "inspection_created",
            f"the complaint state moved on ({after['state']})",
        )
        check(inspection_id in after["linked_inspections"], "the complaint links to the inspection")

        print("evidence analysis")
        job_ids = [item["id"] for item in inspection["jobs"]]
        check(bool(job_ids), "an analysis job was queued for the promoted evidence")
        for job_id in job_ids:
            for _ in range(90):
                time.sleep(2)
                job = officer.get(f"/api/v1/jobs/{job_id}").json()
                if job["state"] in {"succeeded", "failed"}:
                    break
            check(job["state"] == "succeeded", f"analysis finished ({job['state']})")

        detail = officer.get(f"/api/v1/inspections/{inspection_id}").json()
        check(len(detail["candidates"]) >= 5, f"{len(detail['candidates'])} readings produced")

        print("review and checks")
        for candidate in detail["candidates"]:
            live = officer.get(f"/api/v1/inspections/{inspection_id}").json()
            item = next((row for row in live["candidates"] if row["id"] == candidate["id"]), None)
            if item is None or item["review_state"] != "pending":
                continue
            officer.patch(
                f"/api/v1/candidates/{item['id']}",
                headers=headers,
                json={
                    "review_state": "confirmed",
                    "review_note": "Matches the photograph supplied by the consumer.",
                    "expected_version": item["version"],
                },
            )

        checks = officer.post(f"/api/v1/inspections/{inspection_id}/checks", headers=headers)
        check(checks.status_code == 200, f"checks ran ({checks.status_code})")
        outcomes = checks.json()["outcomes"]
        check(
            outcomes.get("non_compliant", 0) >= 1,
            f"the wrong unit price was found ({outcomes})",
        )

        detail = officer.get(f"/api/v1/inspections/{inspection_id}").json()
        officer.post(
            f"/api/v1/inspections/{inspection_id}/transitions",
            headers=headers,
            json={"target_state": "reviewer_review", "expected_version": detail["version"]},
        )

    # ------------------------------------------------------------------
    # Decision, report, case, notice
    # ------------------------------------------------------------------
    print("decision and report")
    with RefreshingClient(base_url=BASE_URL, timeout=300) as controller:
        controller.post(
            "/api/v1/auth/sign-in", json={"email": controller_email, "password": PASSWORD}
        )
        headers = csrf(controller)

        detail = controller.get(f"/api/v1/inspections/{inspection_id}").json()
        check(
            detail["reference"] is not None,
            "a state controller can read a district inspection beneath it",
        )

        decided = controller.post(
            f"/api/v1/inspections/{inspection_id}/decision",
            headers=headers,
            json={
                "decision": "violation_found",
                "note": (
                    "The printed unit sale price of Rs 5.00 per 100 g does not agree "
                    "with the declared retail sale price of Rs 24.00 and net quantity "
                    "of 1 kg, which give Rs 2.40 per 100 g."
                ),
                "expected_version": detail["version"],
            },
        )
        check(
            decided.status_code == 200,
            f"decision recorded ({decided.status_code}: {decided.text[:160]})",
        )

        issued = controller.post(f"/api/v1/reports/inspections/{inspection_id}", headers=headers)
        check(issued.status_code == 201, f"report issued ({issued.status_code})")

        print("case opened from the report")
        opened = controller.post(
            "/api/v1/cases",
            headers=headers,
            json={
                "inspection_id": inspection_id,
                "respondent_name": "Riverside Foods Private Limited",
                "respondent_role": "manufacturer",
                "respondent_address": "Plot 8, Industrial Estate, Sample City 122001",
                "subject": "Unit sale price inconsistent with declared price and quantity",
            },
        )
        check(opened.status_code == 201, f"case opened ({opened.status_code}: {opened.text[:200]})")
        if opened.status_code != 201:
            return 1
        case = opened.json()
        case_id = case["id"]
        check(case["reference"].startswith("CASE-"), f"case reference issued ({case['reference']})")
        check(case["state"] == "draft", f"a case starts as a draft ({case['state']})")
        check(
            bool(case["legal_basis"]),
            f"the provisions relied on are recorded ({len(case['legal_basis'])})",
        )
        check(
            all("citation" in item for item in case["legal_basis"]),
            "each provision carries its citation",
        )

        print("notice prepared, issued, delivered, answered")
        prepared = controller.post(
            f"/api/v1/cases/{case_id}/notices",
            headers=headers,
            json={
                "notice_type": "show_cause",
                "response_days": 15,
                "signature_block": "State Controller of Legal Metrology (sample data)",
            },
        )
        check(
            prepared.status_code == 201,
            f"notice prepared ({prepared.status_code}: {prepared.text[:200]})",
        )
        notice = prepared.json()
        notice_id = notice["id"]
        check(not notice["is_frozen"], "a prepared notice is not yet frozen")
        check(notice["issued_at"] is None, "a prepared notice is not yet issued")
        check(
            "Riverside Foods Private Limited" in notice["rendered_body"],
            "the respondent appears in the rendered notice",
        )
        check(
            case["reference"] in notice["rendered_body"],
            "the case reference appears in the rendered notice",
        )
        check(
            "[not recorded]" not in notice["rendered_body"],
            "no placeholder was left unfilled",
        )
        check(len(notice["body_sha256"]) == 64, "the notice text is hashed")

        case = controller.get(f"/api/v1/cases/{case_id}").json()
        due = (date.today() + timedelta(days=15)).isoformat()
        issued_notice = controller.post(
            f"/api/v1/cases/{case_id}/notices/{notice_id}/issue",
            headers=headers,
            json={"response_due_on": due, "expected_version": case["version"]},
        )
        check(
            issued_notice.status_code == 200,
            f"notice issued ({issued_notice.status_code}: {issued_notice.text[:200]})",
        )
        case = issued_notice.json()
        served = case["notices"][0]
        check(served["is_frozen"], "the served notice is frozen")
        check(
            served["response_due_on"] == due,
            f"the response deadline is recorded ({served['response_due_on']})",
        )
        check(case["state"] == "notice_issued", f"case state moved on ({case['state']})")

        past = (date.today() - timedelta(days=1)).isoformat()
        bad_due = controller.post(
            f"/api/v1/cases/{case_id}/notices/{notice_id}/issue",
            headers=headers,
            json={"response_due_on": past, "expected_version": case["version"]},
        )
        check(
            bad_due.status_code == 409,
            f"re-issuing an issued notice is refused ({bad_due.status_code})",
        )

        delivered = controller.post(
            f"/api/v1/cases/{case_id}/notices/{notice_id}/delivery",
            headers=headers,
            json={
                "method": "registered_post",
                "delivered_at": datetime.now(UTC).isoformat(),
                "proof_reference": "RP-SAMPLE-0001",
                "note": "Acknowledgement slip retained on file.",
                "expected_version": case["version"],
            },
        )
        check(
            delivered.status_code == 200,
            f"delivery recorded ({delivered.status_code}: {delivered.text[:160]})",
        )
        case = delivered.json()
        check(case["state"] == "awaiting_response", f"case awaits a response ({case['state']})")

        answered = controller.post(
            f"/api/v1/cases/{case_id}/notices/{notice_id}/response",
            headers=headers,
            json={
                "summary": (
                    "The manufacturer states the unit price was a printing error and "
                    "that corrected packaging is in production."
                ),
                "received_at": datetime.now(UTC).isoformat(),
                "expected_version": case["version"],
            },
        )
        check(answered.status_code == 200, f"response recorded ({answered.status_code})")
        case = answered.json()
        check(case["state"] == "response_received", f"case state moved on ({case['state']})")
        check(len(case["events"]) >= 2, f"the case timeline has entries ({len(case['events'])})")

        print("case outcome")
        considered = controller.post(
            f"/api/v1/cases/{case_id}/transitions",
            headers=headers,
            json={
                "target_state": "under_consideration",
                "reason": "Considering the response.",
                "expected_version": case["version"],
            },
        )
        check(considered.status_code == 200, f"moved to consideration ({considered.status_code})")
        case = considered.json()
        outcome = controller.post(
            f"/api/v1/cases/{case_id}/outcome",
            headers=headers,
            json={
                "outcome": "advisory_issued",
                "outcome_note": (
                    "Corrected packaging accepted. A compliance advisory was issued and "
                    "a follow-up inspection scheduled."
                ),
                "expected_version": case["version"],
            },
        )
        check(
            outcome.status_code == 200,
            f"outcome recorded ({outcome.status_code}: {outcome.text[:160]})",
        )
        check(outcome.json()["state"] == "resolved", "case resolved")

        print("audit trail")
        trail = controller.get("/api/v1/audit", params={"page_size": 100}).json()
        check(trail["meta"]["total"] >= 25, f"{trail['meta']['total']} audit events recorded")
        actions = {item["action"] for item in trail["items"]}
        for expected in (
            "complaint.received",
            "complaint.converted_to_inspection",
            "evidence.uploaded",
            "candidate.reviewed",
            "inspection.checks_run",
            "inspection.state_changed",
            "report.issued",
            "case.opened",
            "notice.issued",
        ):
            check(expected in actions, f"'{expected}' is recorded in the audit trail")

        first = trail["items"][0]
        check(len(first["event_hash"]) == 64, "each event carries a hash")
        check(first["previous_hash"] is not None, "each event links to its predecessor")

        verified = controller.get("/api/v1/audit/verify").json()
        check(verified["intact"], f"the audit chain verifies ({verified.get('problem')})")
        check(
            verified["events_checked"] == verified["head_sequence"],
            f"every event was replayed ({verified['events_checked']} of {verified['head_sequence']})",
        )

        entity = controller.get(f"/api/v1/audit/entity/inspection/{inspection_id}").json()
        check(
            len(entity["events"]) >= 3,
            f"the inspection's own history is retrievable ({len(entity['events'])})",
        )
        check(len(entity["state_transitions"]) >= 4, "state transitions are retrievable")

        export = controller.get("/api/v1/audit/export.csv")
        check(export.status_code == 200, f"the audit trail exports as CSV ({export.status_code})")
        check("event_hash" in export.text.splitlines()[0], "the export includes the hashes")

    # ------------------------------------------------------------------
    # Tamper detection
    # ------------------------------------------------------------------
    print("audit tamper detection")
    from sqlalchemy import create_engine, text

    from app.config import get_settings

    engine = create_engine(get_settings().sync_database_url)
    with engine.begin() as connection:
        blocked = False
        try:
            connection.execute(
                text("UPDATE audit_events SET reason = 'tampered' WHERE sequence = 1")
            )
        except Exception as exc:
            blocked = "append-only" in str(exc)
        check(blocked, "the database refuses to alter a recorded audit event")

    with engine.begin() as connection:
        head = connection.execute(
            text("SELECT max(sequence) FROM audit_events WHERE chain_key = 'global'")
        ).scalar_one()
        # Insert a row with a hash that does not match its contents. INSERT is
        # permitted (the table is append-only, not read-only), so this is exactly the
        # attack the chain is meant to expose.
        connection.execute(
            text(
                "INSERT INTO audit_events (id, chain_key, sequence, previous_hash, "
                "event_hash, action, entity_type, actor_is_public, old_values, "
                "new_values, recorded_at) VALUES (:id, 'global', :seq, :prev, :hash, "
                "'forged.event', 'system', false, '{}'::jsonb, '{}'::jsonb, now())"
            ),
            {
                "id": str(uuid.uuid4()),
                "seq": head + 1,
                "prev": "f" * 64,
                "hash": "0" * 64,
            },
        )
    engine.dispose()

    with RefreshingClient(base_url=BASE_URL, timeout=60) as controller:
        controller.post(
            "/api/v1/auth/sign-in", json={"email": controller_email, "password": PASSWORD}
        )
        result = controller.get("/api/v1/audit/verify").json()
        check(not result["intact"], "the chain verification detects a forged event")
        check(
            result["first_broken_sequence"] == head + 1,
            f"it names the offending sequence ({result['first_broken_sequence']} vs {head + 1})",
        )
        check(
            "previous_hash" in (result["problem"] or "").lower()
            or "does not match" in (result["problem"] or "").lower(),
            f"it explains what is wrong ({result['problem']})",
        )
        check(
            "compromised" in result["note"].lower(),
            "the response tells the reader to treat the trail as compromised",
        )

    print()
    if failures:
        print(f"{len(failures)} of {count} checks FAILED")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"all {count} complaint, case and audit checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
