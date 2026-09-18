"""Verify the security core: hashing, policy, tokens, permissions, jurisdiction.

Run inside the api image:
    docker compose run --rm --no-deps migrate python scripts/verify_security.py
"""

from __future__ import annotations

import sys
import time
import uuid

from app.config import get_settings
from app.domain.enums import Role
from app.errors import NotFoundError, PermissionDeniedError, SessionExpiredError
from app.security import passwords, permissions, tokens

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


def verify_passwords() -> None:
    print("password hashing")
    secret = "Correct-Horse-Battery-7"
    digest = passwords.hash_password(secret)
    check(digest.startswith("$argon2id$"), f"Argon2id used ({digest[:14]}...)")
    check(secret not in digest, "plaintext absent from the hash")

    matched, needs_rehash = passwords.verify_password(secret, digest)
    check(matched and not needs_rehash, "correct password verifies without rehash")
    check(
        passwords.verify_password("wrong-password-x", digest) == (False, False),
        "wrong password rejected",
    )
    check(
        passwords.verify_password(secret, "not-a-hash") == (False, False),
        "malformed hash treated as mismatch",
    )
    check(passwords.hash_password(secret) != digest, "salt differs between hashes")

    # Timing equalisation: unknown-account path must cost the same order of
    # magnitude as a real verification.
    start = time.perf_counter()
    passwords.verify_password("wrong-password-x", digest)
    real = time.perf_counter() - start
    start = time.perf_counter()
    passwords.dummy_verify()
    dummy = time.perf_counter() - start
    ratio = dummy / real if real else 0
    check(0.3 <= ratio <= 3.0, f"dummy_verify cost within 3x of real verify (ratio {ratio:.2f})")

    print("password policy")
    check(
        passwords.check_policy("Correct-Horse-Battery-7").acceptable, "strong passphrase accepted"
    )
    check(not passwords.check_policy("short1!").acceptable, "too-short rejected")
    check(not passwords.check_policy("password").acceptable, "common password rejected")
    check(not passwords.check_policy("aaaaaaaaaaaaaaaa").acceptable, "long repeat run rejected")
    check(
        not passwords.check_policy("ravi.kumar-2026", email="ravi.kumar@example.org").acceptable,
        "password containing the email local part rejected",
    )
    check(passwords.check_policy("aaaa" * 8).acceptable is False, "repeated block rejected")


def verify_tokens() -> None:
    print("access tokens")
    user_id, session_id = uuid.uuid4(), uuid.uuid4()
    token, expires_at = tokens.issue_access_token(
        user_id=user_id, session_id=session_id, role="inspector", jurisdiction_code="IN-HR-GURUGRAM"
    )
    claims = tokens.decode_access_token(token)
    check(claims.user_id == user_id, "subject round-trips")
    check(claims.session_id == session_id, "session id carried in the token")
    check(claims.role == "inspector", "role carried in the token")
    check(claims.jurisdiction_code == "IN-HR-GURUGRAM", "jurisdiction carried in the token")
    ttl = get_settings().access_token_ttl_seconds
    check(0 < claims.seconds_remaining <= ttl, f"expiry within configured TTL ({ttl}s)")

    tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    rejected = False
    try:
        tokens.decode_access_token(tampered)
    except SessionExpiredError:
        rejected = True
    check(rejected, "tampered signature rejected")

    rejected = False
    try:
        tokens.decode_access_token("not.a.token")
    except SessionExpiredError:
        rejected = True
    check(rejected, "malformed token rejected")

    print("opaque refresh tokens")
    refresh = tokens.generate_refresh_token()
    stored = tokens.hash_token(refresh)
    check(len(refresh) >= 40, f"refresh token length {len(refresh)} >= 40")
    check(len(stored) == 64 and stored != refresh, "stored form is a 64-char hash, not the token")
    check(tokens.hash_token(refresh) == stored, "hashing is deterministic")
    check(tokens.tokens_equal(stored, stored), "constant-time comparison accepts a match")
    check(
        not tokens.tokens_equal(tokens.hash_token(tokens.generate_refresh_token()), stored),
        "different token rejected",
    )

    code = tokens.generate_verification_code()
    check(len(code) == 14 and code.count("-") == 2, f"verification code shape ({code})")
    # The alphabet excludes 0/O, 1/I/L, 5/S, B and Z. 8 is retained because B is
    # absent, so there is nothing left to confuse it with.
    check(
        not set(code) & set("015BILOSZ"),
        f"verification code avoids confusable characters ({code})",
    )


def verify_permissions() -> None:
    print("permission matrix")
    check(
        not permissions.has_permission(Role.INSPECTOR, permissions.Permission.INSPECTION_DECIDE),
        "inspector cannot record the final decision",
    )
    check(
        permissions.has_permission(Role.REVIEWER, permissions.Permission.INSPECTION_DECIDE),
        "reviewer can record the final decision",
    )
    check(
        not permissions.has_permission(Role.RULE_ADMIN, permissions.Permission.EVIDENCE_UPLOAD),
        "rule administrator cannot upload evidence",
    )
    check(
        not permissions.has_permission(Role.INSPECTOR, permissions.Permission.RULE_APPROVE),
        "inspector cannot approve rules",
    )
    check(
        not permissions.has_permission(Role.REVIEWER, permissions.Permission.AUDIT_READ),
        "reviewer cannot read the audit trail",
    )
    check(
        permissions.has_permission(Role.CONTROLLER, permissions.Permission.AUDIT_READ),
        "controller can read the audit trail",
    )
    for permission in permissions.Permission:
        if not permissions.has_permission(Role.ADMIN, permission):
            check(False, f"administrator holds {permission.value}")
            break
    else:
        check(True, f"administrator holds all {len(list(permissions.Permission))} permissions")

    raised = False
    try:
        permissions.require_permission(Role.INSPECTOR, permissions.Permission.RULE_APPROVE)
    except PermissionDeniedError:
        raised = True
    check(raised, "require_permission raises for a denied action")

    print("jurisdiction scope")
    inspector = permissions.JurisdictionScope.for_account(Role.INSPECTOR, "IN-HR-GURUGRAM")
    controller = permissions.JurisdictionScope.for_account(Role.CONTROLLER, "IN-HR")
    admin = permissions.JurisdictionScope.for_account(Role.ADMIN, "IN")

    check(inspector.covers("IN-HR-GURUGRAM"), "inspector covers own district")
    check(not inspector.covers("IN-HR-FARIDABAD"), "inspector denied a sibling district")
    check(not inspector.covers("IN-HR"), "inspector denied the parent state")
    check(not inspector.covers("IN-PB-LUDHIANA"), "inspector denied another state")
    check(controller.covers("IN-HR-GURUGRAM"), "controller covers a district beneath it")
    check(controller.covers("IN-HR"), "controller covers its own state")
    check(not controller.covers("IN-PB-LUDHIANA"), "controller denied another state")
    check(not controller.covers("IN"), "controller denied the national level")
    check(admin.unrestricted and admin.covers("IN-PB-LUDHIANA"), "administrator unrestricted")

    # Prefix confusion: IN-HR must not match IN-HRX.
    check(not controller.covers("IN-HRX-SOMEWHERE"), "prefix confusion rejected (IN-HR vs IN-HRX)")

    raised = False
    try:
        inspector.require("IN-PB-LUDHIANA")
    except NotFoundError:
        raised = True
    check(raised, "out-of-scope record raises NotFound, not Forbidden")

    check(
        permissions.normalise_jurisdiction(" in-hr-gurugram ") == "IN-HR-GURUGRAM",
        "jurisdiction codes normalised",
    )

    from app.models.inspection import Inspection

    check(
        admin.filter(Inspection.jurisdiction_code) is None,
        "no SQL filter for an unrestricted scope",
    )
    predicate = str(controller.filter(Inspection.jurisdiction_code))
    check("OR" in predicate.upper(), f"controller filter includes descendants ({predicate[:60]})")
    predicate = str(inspector.filter(Inspection.jurisdiction_code))
    check("=" in predicate, f"inspector filter is an exact match ({predicate[:60]})")

    matrix = permissions.permission_matrix()
    check(set(matrix) == {r.value for r in Role}, "permission matrix covers every role")


def main() -> int:
    verify_passwords()
    verify_tokens()
    verify_permissions()
    print()
    if failures:
        print(f"{len(failures)} of {checks} checks FAILED")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"all {checks} security checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
