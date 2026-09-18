"""Prepare a workspace for demonstration or manual exploration.

Creates one account per role with known credentials, loads the starter rule set, and
takes every rule version through simulation and approval so the workspace is usable
immediately.

    docker compose run --rm --no-deps -e API_URL=http://api:8000 \
        --entrypoint python api scripts/seed_demo.py

Options through the environment:

    DEMO_PASSWORD     password for every account (default below)
    DEMO_EMAIL_DOMAIN domain for the account addresses (default example.org)
    SKIP_RULES=1      create accounts only

These are development credentials for a local workspace. They are printed to the
terminal on purpose, and a real deployment must not use them: there is no public
registration, and every account here is created through the ordinary authenticated
administration endpoint.
"""

from __future__ import annotations

import os
import sys

import httpx

BASE_URL = os.environ.get("API_URL", "http://api:8000")
PASSWORD = os.environ.get("DEMO_PASSWORD", "Ganga-Yamuna-2026")
DOMAIN = os.environ.get("DEMO_EMAIL_DOMAIN", "example.org")
SKIP_RULES = os.environ.get("SKIP_RULES") == "1"

#: (local part, role, jurisdiction code, jurisdiction name, display name, why)
ACCOUNTS: tuple[tuple[str, str, str, str, str, str], ...] = (
    (
        "admin",
        "admin",
        "IN",
        "National workspace",
        "Workspace Administrator",
        "Creates accounts, roles and jurisdictions",
    ),
    (
        "ruleauthor",
        "rule_admin",
        "IN",
        "National workspace",
        "Rule Author",
        "Authors and simulates rule versions",
    ),
    (
        "ruleapprover",
        "rule_admin",
        "IN",
        "National workspace",
        "Rule Approver",
        "Approves rule versions the author cannot approve",
    ),
    (
        "inspector",
        "inspector",
        "IN-HR-GURUGRAM",
        "Gurugram district",
        "District Inspector",
        "Captures evidence and reviews machine readings",
    ),
    (
        "reviewer",
        "reviewer",
        "IN-HR-GURUGRAM",
        "Gurugram district",
        "District Reviewer",
        "Records the decision and issues reports",
    ),
    (
        "controller",
        "controller",
        "IN-HR",
        "Haryana state office",
        "State Controller",
        "Supervises the district, opens cases, reads the audit trail",
    ),
    (
        "otherstate",
        "inspector",
        "IN-PB-LUDHIANA",
        "Ludhiana district",
        "Other State Inspector",
        "Exists to demonstrate jurisdiction isolation",
    ),
)


def address(local: str) -> str:
    return f"{local}@{DOMAIN}"


def csrf(client: httpx.Client) -> dict[str, str]:
    token = client.cookies.get("maanak_csrf")
    return {"X-CSRF-Token": token} if token else {}


def main() -> int:
    admin_email = address("admin")

    with httpx.Client(base_url=BASE_URL, timeout=180) as client:
        status = client.get("/api/v1/auth/status").json()

        if not status.get("initialised"):
            response = client.post(
                "/api/v1/auth/bootstrap",
                json={
                    "email": admin_email,
                    "name": "Workspace Administrator",
                    "password": PASSWORD,
                    "jurisdiction_code": "IN",
                    "jurisdiction_name": "National workspace",
                    "designation": "Workspace administrator",
                },
            )
            if response.status_code != 201:
                print(f"could not create the first administrator: {response.text[:300]}")
                return 1
            print(f"created the first administrator: {admin_email}")
        else:
            print("workspace already initialised")

        signed_in = client.post(
            "/api/v1/auth/sign-in", json={"email": admin_email, "password": PASSWORD}
        )
        if signed_in.status_code != 200:
            print(
                f"could not sign in as {admin_email}: {signed_in.text[:200]}\n"
                "The workspace was initialised with different credentials. Clear it with\n"
                "  docker compose run --rm --no-deps migrate python scripts/reset_data.py\n"
                "and run this again."
            )
            return 1

        created: list[str] = []
        existing: list[str] = []
        for local, role, code, jurisdiction, name, _why in ACCOUNTS:
            if local == "admin":
                continue
            response = client.post(
                "/api/v1/users",
                headers=csrf(client),
                json={
                    "email": address(local),
                    "name": name,
                    "password": PASSWORD,
                    "role": role,
                    "jurisdiction_code": code,
                    "jurisdiction_name": jurisdiction,
                    "must_change_password": False,
                },
            )
            if response.status_code == 201:
                created.append(address(local))
            elif response.status_code == 409:
                existing.append(address(local))
            else:
                print(
                    f"  could not create {address(local)}: {response.status_code} {response.text[:160]}"
                )
        print(f"accounts created: {len(created)}, already present: {len(existing)}")

    if SKIP_RULES:
        print_credentials(rules_ready=False)
        return 0

    # --- Rules ---------------------------------------------------------
    with httpx.Client(base_url=BASE_URL, timeout=300) as author:
        author.post(
            "/api/v1/auth/sign-in", json={"email": address("ruleauthor"), "password": PASSWORD}
        )
        seeded = author.post("/api/v1/rules/seed", headers=csrf(author))
        if seeded.status_code == 200:
            payload = seeded.json()
            print(
                f"starter rules: {len(payload['created'])} created, "
                f"{len(payload['skipped'])} already present (all as drafts)"
            )
        else:
            print(f"could not seed rules: {seeded.status_code} {seeded.text[:200]}")

    # Approval is done by the second rule administrator: the author of a version is
    # refused, which is the separation of duties the workspace enforces.
    with httpx.Client(base_url=BASE_URL, timeout=600) as approver:
        approver.post(
            "/api/v1/auth/sign-in",
            json={"email": address("ruleapprover"), "password": PASSWORD},
        )
        listing = approver.get("/api/v1/rules", params={"page_size": 100})
        if listing.status_code != 200:
            print(f"could not list rules: {listing.status_code}")
            print_credentials(rules_ready=False)
            return 0

        approved = 0
        blocked: list[str] = []
        for item in listing.json()["items"]:
            if item["status"] in {"approved", "active"}:
                approved += 1
                continue

            current = approver.get(f"/api/v1/rules/{item['id']}").json()
            if current["status"] == "draft":
                submitted = approver.post(
                    f"/api/v1/rules/{item['id']}/status",
                    headers=csrf(approver),
                    json={
                        "target_status": "under_review",
                        "expected_version": current["record_version"],
                    },
                )
                if submitted.status_code != 200:
                    blocked.append(f"{item['code']}: submit {submitted.status_code}")
                    continue

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
                    "note": "Reviewed as part of preparing the demonstration workspace.",
                    "expected_version": current["record_version"],
                },
            )
            if result.status_code == 200:
                approved += 1
            else:
                blocked.append(f"{item['code']}: approve {result.status_code}")

        print(f"rule versions approved and usable: {approved}")
        for problem in blocked:
            print(f"  not approved - {problem}")

    print_credentials(rules_ready=True)
    return 0


def print_credentials(*, rules_ready: bool) -> None:
    width = max(len(address(local)) for local, *_ in ACCOUNTS) + 2
    print()
    print("Sign in at http://localhost:8080/login.html")
    print(f"Password for every account below: {PASSWORD}")
    print()
    print(f"{'email':<{width}} {'role':<11} {'jurisdiction':<16} what it is for")
    print("-" * (width + 60))
    for local, role, code, _jurisdiction, _name, why in ACCOUNTS:
        print(f"{address(local):<{width}} {role:<11} {code:<16} {why}")
    print()
    if rules_ready:
        print(
            "The starter rules are approved, so running the checks on an inspection will\n"
            "produce findings. Their citations are still unverified against the gazette,\n"
            "and the workspace shows that on every finding."
        )
    else:
        print(
            "No rule version is approved yet, so the checks will report 'unable to\n"
            "determine' rather than findings. Sign in as the rule approver to approve them."
        )
    print()
    print("These are local development credentials. Do not use them in a real deployment.")


if __name__ == "__main__":
    sys.exit(main())
