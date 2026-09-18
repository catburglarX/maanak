"""Verify the authentication flow against a running API over HTTP.

Run on the compose network so it reaches the API by service name:

    docker compose run --rm --no-deps -e API_URL=http://api:8000 \
        --entrypoint python api scripts/verify_auth_flow.py
"""

from __future__ import annotations

import os
import sys
import uuid

import httpx

BASE_URL = os.environ.get("API_URL", "http://api:8000")
CSRF_COOKIE = "maanak_csrf"
ACCESS_COOKIE = "maanak_access"
REFRESH_COOKIE = "maanak_refresh"

failures: list[str] = []
checks = 0


def check(condition: bool, description: str) -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok    {description}")
    else:
        failures.append(description)
        print(f"  FAIL  {description}")


def csrf_headers(client: httpx.Client) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    return {"X-CSRF-Token": token} if token else {}


def unique_email(label: str) -> str:
    return f"{label}.{uuid.uuid4().hex[:8]}@example.org"


def main() -> int:
    strong_password = "Ganga-Yamuna-River-88"

    print("workspace bootstrap")
    with httpx.Client(base_url=BASE_URL, timeout=30) as client:
        status = client.get("/api/v1/auth/status").json()
        already = bool(status.get("initialised"))
        print(f"  info  workspace already initialised: {already}")

        admin_email = unique_email("admin")
        response = client.post(
            "/api/v1/auth/bootstrap",
            json={
                "email": admin_email,
                "name": "Workspace Administrator",
                "password": strong_password,
                "jurisdiction_code": "IN",
                "jurisdiction_name": "National workspace",
                "designation": "Controller of Legal Metrology",
            },
        )
        if already:
            check(
                response.status_code == 409,
                f"second bootstrap refused (got {response.status_code})",
            )
            # Re-use the existing administrator supplied through the environment.
            admin_email = os.environ["ADMIN_EMAIL"]
        else:
            check(
                response.status_code == 201,
                f"bootstrap created the administrator (got {response.status_code})",
            )
            body = response.json()
            check(body.get("role") == "admin", "bootstrap account has the admin role")
            # Look for the secret itself. Field names such as
            # "must_change_password" legitimately contain the word "password".
            check(
                strong_password not in response.text,
                "bootstrap response does not echo the password value",
            )

            repeat = client.post(
                "/api/v1/auth/bootstrap",
                json={
                    "email": unique_email("second"),
                    "name": "Second Administrator",
                    "password": strong_password,
                    "jurisdiction_code": "IN",
                    "jurisdiction_name": "National workspace",
                },
            )
            check(
                repeat.status_code == 409, f"bootstrap is one-time only (got {repeat.status_code})"
            )
            check(
                repeat.json()["error"]["code"] == "already_initialised",
                "stable error code for repeat bootstrap",
            )

        print("weak password rejection")
        weak = client.post(
            "/api/v1/users",
            json={
                "email": unique_email("weak"),
                "name": "Weak Password",
                "password": "password1234",
                "role": "inspector",
                "jurisdiction_code": "IN-HR-GURUGRAM",
                "jurisdiction_name": "Gurugram",
            },
        )
        check(
            weak.status_code == 401,
            f"unauthenticated account creation refused (got {weak.status_code})",
        )

    print("sign-in")
    with httpx.Client(base_url=BASE_URL, timeout=30) as client:
        wrong = client.post(
            "/api/v1/auth/sign-in", json={"email": admin_email, "password": "definitely-wrong-1"}
        )
        check(wrong.status_code == 401, f"wrong password rejected (got {wrong.status_code})")
        wrong_body = wrong.json()["error"]
        unknown = client.post(
            "/api/v1/auth/sign-in",
            json={"email": unique_email("nobody"), "password": "definitely-wrong-1"},
        )
        unknown_body = unknown.json()["error"]
        check(
            wrong_body["message"] == unknown_body["message"]
            and wrong_body["code"] == unknown_body["code"],
            "unknown account and wrong password give an identical error",
        )
        check(ACCESS_COOKIE not in client.cookies, "no session cookie issued on failure")

        good = client.post(
            "/api/v1/auth/sign-in", json={"email": admin_email, "password": strong_password}
        )
        check(good.status_code == 200, f"sign-in succeeded (got {good.status_code})")
        check(ACCESS_COOKIE in client.cookies, "access cookie set")
        check(REFRESH_COOKIE in client.cookies, "refresh cookie set")
        check(CSRF_COOKIE in client.cookies, "csrf cookie set")

        set_cookie_headers = " ".join(good.headers.get_list("set-cookie")).lower()
        access_directives = [
            part for part in good.headers.get_list("set-cookie") if part.startswith(ACCESS_COOKIE)
        ]
        check(
            bool(access_directives) and "httponly" in access_directives[0].lower(),
            "access cookie is HttpOnly",
        )
        refresh_directives = [
            part for part in good.headers.get_list("set-cookie") if part.startswith(REFRESH_COOKIE)
        ]
        check(
            bool(refresh_directives) and "httponly" in refresh_directives[0].lower(),
            "refresh cookie is HttpOnly",
        )
        check(
            bool(refresh_directives) and "path=/api/v1/auth" in refresh_directives[0].lower(),
            "refresh cookie is path-restricted to the auth endpoints",
        )
        check("samesite=lax" in set_cookie_headers, "cookies set SameSite")

        body = good.json()
        check(body["user"]["email"] == admin_email, "profile returned for the signed-in account")
        check(
            "permissions" in body["user"] and body["user"]["permissions"],
            "profile includes permissions",
        )
        check(
            strong_password not in good.text, "sign-in response does not contain the password value"
        )

        print("token in body check")
        check(
            "access_token" not in good.text and "refresh_token" not in good.text,
            "no bearer token returned in the response body",
        )

        print("authenticated read")
        me = client.get("/api/v1/auth/me")
        check(me.status_code == 200, f"GET /auth/me succeeded (got {me.status_code})")
        check(me.json()["email"] == admin_email, "me returns the right account")

        print("csrf enforcement")
        no_csrf = client.post(
            "/api/v1/users",
            json={
                "email": unique_email("nocsrf"),
                "name": "No CSRF Header",
                "password": strong_password,
                "role": "inspector",
                "jurisdiction_code": "IN-HR-GURUGRAM",
                "jurisdiction_name": "Gurugram",
            },
        )
        check(
            no_csrf.status_code == 403,
            f"unsafe request without CSRF header refused (got {no_csrf.status_code})",
        )
        check(no_csrf.json()["error"]["code"] == "csrf_token_missing", "stable csrf error code")

        bad_csrf = client.post(
            "/api/v1/users",
            headers={"X-CSRF-Token": "not-the-right-token"},
            json={
                "email": unique_email("badcsrf"),
                "name": "Bad CSRF Header",
                "password": strong_password,
                "role": "inspector",
                "jurisdiction_code": "IN-HR-GURUGRAM",
                "jurisdiction_name": "Gurugram",
            },
        )
        check(
            bad_csrf.status_code == 403,
            f"mismatched CSRF token refused (got {bad_csrf.status_code})",
        )

        print("account creation")
        headers = csrf_headers(client)
        inspector_email = unique_email("inspector")
        created = client.post(
            "/api/v1/users",
            headers=headers,
            json={
                "email": inspector_email,
                "name": "District Inspector",
                "password": strong_password,
                "role": "inspector",
                "jurisdiction_code": "in-hr-gurugram",
                "jurisdiction_name": "Gurugram district",
                "must_change_password": False,
            },
        )
        check(
            created.status_code == 201,
            f"inspector created (got {created.status_code}: {created.text[:120]})",
        )
        inspector = created.json()
        check(
            inspector["jurisdiction_code"] == "IN-HR-GURUGRAM",
            f"jurisdiction normalised to upper case (got {inspector['jurisdiction_code']})",
        )

        duplicate = client.post(
            "/api/v1/users",
            headers=headers,
            json={
                "email": inspector_email.upper(),
                "name": "Duplicate Email",
                "password": strong_password,
                "role": "inspector",
                "jurisdiction_code": "IN-HR-GURUGRAM",
                "jurisdiction_name": "Gurugram district",
            },
        )
        check(
            duplicate.status_code == 409,
            f"duplicate email refused case-insensitively (got {duplicate.status_code})",
        )

        weak = client.post(
            "/api/v1/users",
            headers=headers,
            json={
                "email": unique_email("weak2"),
                "name": "Weak Password",
                "password": "password1234",
                "role": "inspector",
                "jurisdiction_code": "IN-HR-GURUGRAM",
                "jurisdiction_name": "Gurugram district",
            },
        )
        check(weak.status_code == 422, f"common password refused (got {weak.status_code})")

        mass_assignment = client.post(
            "/api/v1/users",
            headers=headers,
            json={
                "email": unique_email("mass"),
                "name": "Mass Assignment",
                "password": strong_password,
                "role": "inspector",
                "jurisdiction_code": "IN-HR-GURUGRAM",
                "jurisdiction_name": "Gurugram district",
                "is_active": True,
                "password_hash": "$argon2id$injected",
            },
        )
        check(
            mass_assignment.status_code == 422,
            f"unexpected fields refused, blocking mass assignment (got {mass_assignment.status_code})",
        )

        print("refresh rotation")
        original_refresh = client.cookies.get(REFRESH_COOKIE)
        refreshed = client.post("/api/v1/auth/refresh")
        check(refreshed.status_code == 200, f"refresh succeeded (got {refreshed.status_code})")
        rotated_refresh = client.cookies.get(REFRESH_COOKIE)
        check(
            rotated_refresh is not None and rotated_refresh != original_refresh,
            "refresh token was rotated",
        )

        print("refresh reuse detection")
        with httpx.Client(base_url=BASE_URL, timeout=30) as attacker:
            attacker.cookies.set(REFRESH_COOKIE, original_refresh, path="/api/v1/auth")
            replay = attacker.post("/api/v1/auth/refresh")
            check(
                replay.status_code == 401,
                f"replayed refresh token rejected (got {replay.status_code})",
            )
            check(
                replay.json()["error"]["code"] in {"session_revoked", "session_expired"},
                "replay reported as a revoked session",
            )

        # The whole family is revoked, so the legitimate holder is also signed out.
        after_replay = client.get("/api/v1/auth/me")
        check(
            after_replay.status_code == 401,
            f"reuse detection revoked the whole session family (got {after_replay.status_code})",
        )

    print("session revocation takes effect immediately")
    with httpx.Client(base_url=BASE_URL, timeout=30) as admin:
        admin.post("/api/v1/auth/sign-in", json={"email": admin_email, "password": strong_password})
        with httpx.Client(base_url=BASE_URL, timeout=30) as officer:
            officer.post(
                "/api/v1/auth/sign-in",
                json={"email": inspector_email, "password": strong_password},
            )
            check(officer.get("/api/v1/auth/me").status_code == 200, "inspector signed in")

            revoked = admin.delete(
                f"/api/v1/users/{inspector['id']}/sessions", headers=csrf_headers(admin)
            )
            check(
                revoked.status_code == 200,
                f"administrator revoked sessions (got {revoked.status_code})",
            )
            check(
                officer.get("/api/v1/auth/me").status_code == 401,
                "revoked session is rejected on the next request",
            )

        print("permission enforcement")
        with httpx.Client(base_url=BASE_URL, timeout=30) as officer:
            officer.post(
                "/api/v1/auth/sign-in",
                json={"email": inspector_email, "password": strong_password},
            )
            denied = officer.post(
                "/api/v1/users",
                headers=csrf_headers(officer),
                json={
                    "email": unique_email("escalate"),
                    "name": "Privilege Escalation",
                    "password": strong_password,
                    "role": "admin",
                    "jurisdiction_code": "IN",
                    "jurisdiction_name": "National",
                },
            )
            check(
                denied.status_code == 403,
                f"inspector cannot create accounts (got {denied.status_code})",
            )
            check(
                denied.json()["error"]["code"] == "permission_denied",
                "stable permission error code",
            )
            listing = officer.get("/api/v1/users")
            check(
                listing.status_code == 403,
                f"inspector cannot list accounts (got {listing.status_code})",
            )

        print("sign-out")
        with httpx.Client(base_url=BASE_URL, timeout=30) as officer:
            officer.post(
                "/api/v1/auth/sign-in",
                json={"email": inspector_email, "password": strong_password},
            )
            out = officer.post("/api/v1/auth/sign-out", headers=csrf_headers(officer))
            check(out.status_code == 200, f"sign-out succeeded (got {out.status_code})")
            check(
                officer.get("/api/v1/auth/me").status_code == 401,
                "session unusable after sign-out",
            )

    print("security headers")
    with httpx.Client(base_url=BASE_URL, timeout=30) as client:
        response = client.get("/health/live")
        for header, expected in (
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "same-origin"),
            ("Cache-Control", "no-store"),
        ):
            check(response.headers.get(header) == expected, f"{header}: {expected}")
        check("Content-Security-Policy" in response.headers, "Content-Security-Policy present")
        check("X-Request-ID" in response.headers, "X-Request-ID returned")

    print("error shape")
    with httpx.Client(base_url=BASE_URL, timeout=30) as client:
        missing = client.get("/api/v1/users/00000000-0000-0000-0000-000000000000")
        body = missing.json()
        check("error" in body and "code" in body["error"], "errors use the documented envelope")
        check("request_id" in body["error"], "errors carry the request id")
        check(
            "traceback" not in missing.text.lower() and "sqlalchemy" not in missing.text.lower(),
            "errors do not leak internals",
        )

    print()
    if failures:
        print(f"{len(failures)} of {checks} checks FAILED")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"all {checks} auth-flow checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
