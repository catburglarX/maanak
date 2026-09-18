"""Confirm every demo account can sign in and report its effective scope.

    docker compose run --rm --no-deps -e API_URL=http://api:8000 \
        --entrypoint python api scripts/check_demo_logins.py
"""

from __future__ import annotations

import os
import sys

import httpx

BASE_URL = os.environ.get("API_URL", "http://api:8000")
PASSWORD = os.environ.get("DEMO_PASSWORD", "Ganga-Yamuna-2026")
DOMAIN = os.environ.get("DEMO_EMAIL_DOMAIN", "example.org")

LOCALS = (
    "admin",
    "ruleauthor",
    "ruleapprover",
    "inspector",
    "reviewer",
    "controller",
    "otherstate",
)


def main() -> int:
    failures = 0
    for local in LOCALS:
        email = f"{local}@{DOMAIN}"
        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            response = client.post(
                "/api/v1/auth/sign-in", json={"email": email, "password": PASSWORD}
            )
            if response.status_code != 200:
                print(f"FAIL  {email}: {response.status_code} {response.text[:110]}")
                failures += 1
                continue
            profile = client.get("/api/v1/auth/me").json()
            print(
                f"ok    {email:<26} role={profile['role']:<11} "
                f"scope={profile['scope']:<22} permissions={len(profile['permissions'])}"
            )

    print()
    if failures:
        print(f"{failures} of {len(LOCALS)} accounts could not sign in")
        return 1
    print(f"all {len(LOCALS)} demo accounts sign in successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
