"""Access tokens and opaque refresh tokens.

Two different mechanisms, on purpose:

Access token
    A short-lived signed JWT carried in an ``HttpOnly`` cookie. It is stateless so
    that ordinary requests need no session table read. It carries a session id so
    a revoked session can still be rejected on sensitive operations.

Refresh token
    A long random opaque string. Only its SHA-256 hash is stored. Nothing is
    encoded in the token itself, so a leaked database cannot be used to mint a
    working refresh token.

Key rotation: tokens are signed with the ``current`` key and verified against
``current`` then ``previous``, so rotating ``JWT_SECRET`` does not end active
sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from ..config import get_settings
from ..errors import SessionExpiredError

ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - a token type label, not a secret
REFRESH_TOKEN_BYTES = 32
COOKIE_ACCESS = "maanak_access"
COOKIE_REFRESH = "maanak_refresh"
COOKIE_CSRF = "maanak_csrf"


@dataclass(frozen=True)
class AccessClaims:
    """Validated contents of an access token."""

    user_id: uuid.UUID
    session_id: uuid.UUID
    role: str
    jurisdiction_code: str
    issued_at: datetime
    expires_at: datetime
    token_id: str

    @property
    def seconds_remaining(self) -> int:
        return max(0, int((self.expires_at - datetime.now(UTC)).total_seconds()))


def issue_access_token(
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    role: str,
    jurisdiction_code: str,
) -> tuple[str, datetime]:
    """Return ``(token, expires_at)`` signed with the current key."""
    settings = get_settings()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.access_token_ttl_seconds)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "sid": str(session_id),
        "role": role,
        "jur": jurisdiction_code,
        "typ": ACCESS_TOKEN_TYPE,
        "iss": settings.jwt_issuer,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_urlsafe(12),
    }
    token = jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm=ALGORITHM,
        headers={"kid": "current"},
    )
    return token, expires_at


def decode_access_token(token: str) -> AccessClaims:
    """Validate an access token against every accepted signing key.

    Raises ``SessionExpiredError`` for anything that is not a currently valid
    access token. The caller must not distinguish the reason to the client.
    """
    settings = get_settings()
    keys = settings.signing_keys()

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise SessionExpiredError from exc

    key_id = header.get("kid")
    candidates = [keys[key_id]] if key_id in keys else list(keys.values())

    payload: dict[str, Any] | None = None
    for secret in candidates:
        try:
            payload = jwt.decode(
                token,
                secret,
                algorithms=[ALGORITHM],
                issuer=settings.jwt_issuer,
                options={"require": ["exp", "iat", "sub", "iss"]},
            )
            break
        except jwt.PyJWTError:
            continue

    if payload is None:
        raise SessionExpiredError

    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        raise SessionExpiredError

    try:
        return AccessClaims(
            user_id=uuid.UUID(str(payload["sub"])),
            session_id=uuid.UUID(str(payload["sid"])),
            role=str(payload["role"]),
            jurisdiction_code=str(payload.get("jur", "")),
            issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=UTC),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
            token_id=str(payload.get("jti", "")),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise SessionExpiredError from exc


def generate_refresh_token() -> str:
    """A high-entropy opaque token. Never stored in this form."""
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def hash_token(token: str) -> str:
    """SHA-256 hex digest used as the stored form of an opaque token.

    A plain hash is correct here: the input is 256 bits of random data, so there
    is nothing to brute-force and no need for a slow KDF.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(candidate_hash: str, stored_hash: str) -> bool:
    return hmac.compare_digest(candidate_hash, stored_hash)


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def generate_verification_code() -> str:
    """Short, unambiguous code printed on reports for manual verification.

    Excludes characters that are easily confused in print: 0/O, 1/I/L, 5/S, 8/B.
    """
    alphabet = "ACDEFGHJKMNPQRTUVWXY2346789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def generate_reference_suffix(length: int = 6) -> str:
    alphabet = "ACDEFGHJKMNPQRTUVWXY2346789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
