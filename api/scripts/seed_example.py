"""Build one worked example end to end, using the accounts ``seed_demo.py`` creates.

A freshly seeded workspace has accounts and rules but no work in it, so every count
on the overview reads zero and the detail screens have nothing to open. This walks a
single case through the whole path so that the workspace shows something real:

    consumer complaint -> triage -> inspection -> evidence -> OCR -> officer review
    -> checks -> decision -> report -> case -> notice served

Run it after ``seed_demo.py``:

    docker compose run --rm --no-deps -e API_URL=http://api:8000 \
        --entrypoint python api scripts/seed_example.py

Everything it creates is marked as sample data. The label image is generated, the
company and contact details are invented, and the addresses are on example.org.
Nothing here refers to a real product, premises, brand or person.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta

import httpx

# The label generator lives with the matters suite. Reused rather than duplicated so
# there is one definition of the sample panel.
from session_client import RefreshingClient
from verify_matters import make_label

BASE_URL = os.environ.get("API_URL", "http://api:8000")
PASSWORD = os.environ.get("DEMO_PASSWORD", "Ganga-Yamuna-2026")
DOMAIN = os.environ.get("DEMO_EMAIL_DOMAIN", "example.org")

CONSUMER_EMAIL = f"consumer.sample@{DOMAIN}"


def address(local: str) -> str:
    return f"{local}@{DOMAIN}"


def csrf(client: httpx.Client) -> dict[str, str]:
    token = client.cookies.get("maanak_csrf")
    return {"X-CSRF-Token": token} if token else {}


def step(message: str) -> None:
    print(f"  {message}")


def fail(message: str) -> int:
    print(f"  stopped: {message}")
    return 1


def sign_in(client: httpx.Client, local: str) -> bool:
    response = client.post(
        "/api/v1/auth/sign-in", json={"email": address(local), "password": PASSWORD}
    )
    return response.status_code == 200


def submit_complaint(client: httpx.Client) -> str | None:
    submission = {
        "product_name": "Riverside Iodised Salt 1 kg (sample data)",
        "brand": "Riverside",
        "category": "mrp_overcharge",
        "description": (
            "The shop charged 30 rupees but the packet shows 24 rupees as the maximum "
            "retail price, and the printed unit price does not agree with either. "
            "Submitted as sample data."
        ),
        "purchase_date": date.today().isoformat(),
        "seller_name": "Sample Retail Store",
        "stated_mrp": "24.00",
        "stated_price_paid": "30.00",
        "location_text": "Sample City",
        "contact_email": CONSUMER_EMAIL,
        "consent_to_contact": True,
        "privacy_notice_acknowledged": True,
    }
    response = client.post(
        "/api/v1/public/complaints",
        data={"complaint": json.dumps(submission)},
        files={"package_images": ("packet.jpg", make_label(), "image/jpeg")},
    )
    if response.status_code != 201:
        print(f"  complaint refused: {response.status_code} {response.text[:220]}")
        return None
    reference = response.json()["reference"]
    step(f"complaint submitted: {reference}")
    return reference


def wait_for_jobs(client: httpx.Client, job_ids: list[str]) -> bool:
    for job_id in job_ids:
        for _ in range(90):
            time.sleep(2)
            job = client.get(f"/api/v1/jobs/{job_id}").json()
            if job["state"] in {"succeeded", "failed"}:
                break
        if job["state"] != "succeeded":
            print(f"  analysis job {job_id} ended {job['state']}")
            return False
    return True


def main() -> int:
    print("building a worked example")

    with RefreshingClient(base_url=BASE_URL, timeout=180) as public:
        reference = submit_complaint(public)
    if reference is None:
        return 1

    inspection_id: str | None = None
    with RefreshingClient(base_url=BASE_URL, timeout=600) as officer:
        if not sign_in(officer, "inspector"):
            return fail("could not sign in as the inspector; run seed_demo.py first")
        headers = csrf(officer)

        queue = officer.get("/api/v1/complaints", params={"unassigned": True}).json()
        entry = next(
            (item for item in queue["items"] if item["reference"] == reference),
            None,
        )
        if entry is None:
            return fail("the complaint did not reach the queue")

        detail = officer.get(f"/api/v1/complaints/{entry['id']}").json()
        triaged = officer.post(
            f"/api/v1/complaints/{entry['id']}/triage",
            headers=headers,
            json={
                "jurisdiction_code": "IN-HR-GURUGRAM",
                "jurisdiction_name": "Gurugram district",
                "triage_note": "Standard retail pack; worth inspecting. Sample data.",
                "expected_version": detail["version"],
            },
        )
        if triaged.status_code != 200:
            return fail(f"triage refused ({triaged.status_code})")
        step("complaint triaged into Gurugram district")

        detail = officer.get(f"/api/v1/complaints/{entry['id']}").json()
        opened = officer.post(
            f"/api/v1/complaints/{entry['id']}/inspection",
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
        if opened.status_code != 201:
            return fail(f"inspection could not be opened ({opened.status_code})")
        inspection = opened.json()
        inspection_id = inspection["id"]
        step(f"inspection opened: {inspection['reference']}")

        if not wait_for_jobs(officer, [job["id"] for job in inspection["jobs"]]):
            return fail("evidence analysis did not finish")
        step("evidence analysed and declarations located")

        live = officer.get(f"/api/v1/inspections/{inspection_id}").json()
        confirmed = 0
        for candidate in live["candidates"]:
            current = officer.get(f"/api/v1/inspections/{inspection_id}").json()
            item = next(
                (row for row in current["candidates"] if row["id"] == candidate["id"]), None
            )
            if item is None or item["review_state"] != "pending":
                continue
            response = officer.patch(
                f"/api/v1/candidates/{item['id']}",
                headers=headers,
                json={
                    "review_state": "confirmed",
                    "review_note": "Matches the panel in the photograph.",
                    "expected_version": item["version"],
                },
            )
            if response.status_code == 200:
                confirmed += 1
        step(f"{confirmed} machine readings confirmed by the inspector")

        checks = officer.post(f"/api/v1/inspections/{inspection_id}/checks", headers=headers)
        if checks.status_code != 200:
            return fail(f"checks refused ({checks.status_code})")
        outcomes = checks.json()["outcomes"]
        step(f"checks run: {outcomes}")

        current = officer.get(f"/api/v1/inspections/{inspection_id}").json()
        officer.post(
            f"/api/v1/inspections/{inspection_id}/transitions",
            headers=headers,
            json={"target_state": "reviewer_review", "expected_version": current["version"]},
        )
        step("sent to the reviewer")

    report_reference: str | None = None
    with RefreshingClient(base_url=BASE_URL, timeout=600) as reviewer:
        if not sign_in(reviewer, "reviewer"):
            return fail("could not sign in as the reviewer")
        headers = csrf(reviewer)

        current = reviewer.get(f"/api/v1/inspections/{inspection_id}").json()
        decided = reviewer.post(
            f"/api/v1/inspections/{inspection_id}/decision",
            headers=headers,
            json={
                "decision": "violation_found",
                "note": (
                    "The printed unit sale price of Rs 5.00 per 100 g does not agree with "
                    "the declared retail sale price of Rs 24.00 and net quantity of 1 kg, "
                    "which give Rs 2.40 per 100 g. Recorded on sample data."
                ),
                "expected_version": current["version"],
            },
        )
        if decided.status_code != 200:
            return fail(f"decision refused ({decided.status_code}: {decided.text[:220]})")
        step("decision recorded against the reviewer")

        issued = reviewer.post(f"/api/v1/reports/inspections/{inspection_id}", headers=headers)
        if issued.status_code != 201:
            return fail(f"report refused ({issued.status_code}: {issued.text[:220]})")
        report = issued.json()
        report_reference = report["reference"]
        step(f"report issued: {report_reference}, with a public verification code")

    with RefreshingClient(base_url=BASE_URL, timeout=300) as controller:
        if not sign_in(controller, "controller"):
            return fail("could not sign in as the controller")
        headers = csrf(controller)

        opened = controller.post(
            "/api/v1/cases",
            headers=headers,
            json={
                "inspection_id": inspection_id,
                "respondent_name": "Riverside Foods Private Limited (sample data)",
                "respondent_role": "manufacturer",
                "respondent_address": "Plot 8, Industrial Estate, Sample City 122001",
                "subject": "Unit sale price inconsistent with declared price and quantity",
            },
        )
        if opened.status_code != 201:
            print(f"  case not opened ({opened.status_code}: {opened.text[:220]})")
            return 0
        case = opened.json()
        step(f"case opened: {case['reference']}")

        prepared = controller.post(
            f"/api/v1/cases/{case['id']}/notices",
            headers=headers,
            json={
                "notice_type": "show_cause",
                "response_days": 15,
                "signature_block": "State Controller of Legal Metrology (sample data)",
            },
        )
        if prepared.status_code != 201:
            print(f"  notice not prepared ({prepared.status_code}: {prepared.text[:220]})")
            return 0
        notice = prepared.json()
        step(f"notice prepared: {notice['reference']}")

        # Notice transitions carry the *case* version: the notice is part of the case
        # aggregate, so concurrency is controlled at that level.
        current_case = controller.get(f"/api/v1/cases/{case['id']}").json()
        due = (date.today() + timedelta(days=15)).isoformat()
        served = controller.post(
            f"/api/v1/cases/{case['id']}/notices/{notice['id']}/issue",
            headers=headers,
            json={"response_due_on": due, "expected_version": current_case["version"]},
        )
        if served.status_code != 200:
            print(f"  notice not issued ({served.status_code}: {served.text[:220]})")
            return 0
        step(f"notice issued and frozen, response due {due}")

        current_case = controller.get(f"/api/v1/cases/{case['id']}").json()
        delivered = controller.post(
            f"/api/v1/cases/{case['id']}/notices/{notice['id']}/delivery",
            headers=headers,
            json={
                "method": "registered_post",
                "delivered_at": datetime.now(UTC).isoformat(),
                "proof_reference": "SAMPLE-AD-000123",
                "note": "Acknowledgement due received. Sample data.",
                "expected_version": current_case["version"],
            },
        )
        if delivered.status_code == 200:
            step("service recorded against the notice")
        else:
            print(f"  delivery not recorded ({delivered.status_code}: {delivered.text[:200]})")

    print()
    print("The workspace now holds one complete example, so the overview shows real")
    print("counts and every detail screen has a record to open. Everything in it is")
    print("marked sample data and refers to no real product, premises or person.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
