"""Session lifecycle.

Refresh rotation with reuse detection:

1. Sign-in creates a session row holding the hash of a fresh refresh token, with
   a ``family_id`` identifying the rotation chain.
2. Each refresh marks the current row revoked, creates a successor, and links the
   old row to it through ``rotated_to_id``.
3. Presenting an already-rotated token means the token leaked. The whole family is
   revoked immediately and the caller is signed out.

This is the standard defence for refresh tokens held in browser cookies.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models.user import LoginAttempt, User, UserSession
from .tokens import generate_refresh_token, hash_token


@dataclass(frozen=True)
class IssuedSession:
    """A newly created or rotated session and its plaintext refresh token."""

    session: UserSession
    refresh_token: str


#: Reasons recorded on ``UserSession.revoked_reason``. Kept as constants so the
#: audit trail and the session list in the UI use identical wording.
REVOKED_BY_USER = "signed_out"
REVOKED_BY_ADMIN = "revoked_by_administrator"
REVOKED_ROTATED = "rotated"
REVOKED_REUSE_DETECTED = "reuse_detected"
REVOKED_PASSWORD_CHANGE = "password_changed"  # noqa: S105 - a reason label
REVOKED_ACCOUNT_DISABLED = "account_deactivated"
REVOKED_ROLE_CHANGE = "role_changed"


async def create_session(
    db: AsyncSession,
    *,
    user: User,
    ip_address: str | None,
    user_agent: str | None,
) -> IssuedSession:
    """Start a new session family for a successful sign-in."""
    settings = get_settings()
    now = datetime.now(UTC)
    token = generate_refresh_token()
    session = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        family_id=uuid.uuid4(),
        refresh_token_hash=hash_token(token),
        issued_at=now,
        expires_at=now + timedelta(seconds=settings.refresh_token_ttl_seconds),
        last_seen_at=now,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:400] or None,
    )
    db.add(session)
    await db.flush()
    return IssuedSession(session=session, refresh_token=token)


async def find_session_by_refresh_token(db: AsyncSession, refresh_token: str) -> UserSession | None:
    token_hash = hash_token(refresh_token)
    return await db.scalar(select(UserSession).where(UserSession.refresh_token_hash == token_hash))


async def rotate_session(
    db: AsyncSession,
    *,
    session: UserSession,
    ip_address: str | None,
    user_agent: str | None,
) -> IssuedSession:
    """Replace a session with its successor in the same family."""
    settings = get_settings()
    now = datetime.now(UTC)
    token = generate_refresh_token()

    successor = UserSession(
        id=uuid.uuid4(),
        user_id=session.user_id,
        family_id=session.family_id,
        refresh_token_hash=hash_token(token),
        issued_at=now,
        # A rotation extends the window but never past the family's original
        # absolute lifetime, so a stolen cookie cannot be refreshed forever.
        expires_at=min(
            now + timedelta(seconds=settings.refresh_token_ttl_seconds),
            session.issued_at + timedelta(seconds=settings.refresh_token_ttl_seconds * 2),
        ),
        last_seen_at=now,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:400] or None,
    )
    db.add(successor)
    await db.flush()

    session.revoked_at = now
    session.revoked_reason = REVOKED_ROTATED
    session.rotated_to_id = successor.id
    await db.flush()

    return IssuedSession(session=successor, refresh_token=token)


async def revoke_family(db: AsyncSession, *, family_id: uuid.UUID, reason: str) -> int:
    """Revoke every live session in a rotation chain. Returns the count."""
    now = datetime.now(UTC)
    result = await db.execute(
        update(UserSession)
        .where(UserSession.family_id == family_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
    )
    return int(result.rowcount or 0)


async def revoke_session(db: AsyncSession, *, session_id: uuid.UUID, reason: str) -> bool:
    now = datetime.now(UTC)
    result = await db.execute(
        update(UserSession)
        .where(UserSession.id == session_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
    )
    return bool(result.rowcount)


async def revoke_all_for_user(db: AsyncSession, *, user_id: uuid.UUID, reason: str) -> int:
    now = datetime.now(UTC)
    result = await db.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
    )
    return int(result.rowcount or 0)


async def list_active_sessions(db: AsyncSession, *, user_id: uuid.UUID) -> list[UserSession]:
    now = datetime.now(UTC)
    rows = await db.scalars(
        select(UserSession)
        .where(
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
        .order_by(UserSession.last_seen_at.desc())
    )
    return list(rows)


def session_is_usable(session: UserSession) -> bool:
    """A session is usable when it is live, unexpired and not idle-timed-out."""
    settings = get_settings()
    now = datetime.now(UTC)
    if session.revoked_at is not None:
        return False
    if session.expires_at <= now:
        return False
    idle_limit = timedelta(seconds=settings.session_idle_timeout_seconds)
    return (now - session.last_seen_at) <= idle_limit


async def touch_session(db: AsyncSession, *, session_id: uuid.UUID) -> None:
    """Record activity so idle timeout reflects real use.

    Written with a targeted UPDATE rather than an ORM load so that a normal
    request does not carry a session row through the identity map.
    """
    await db.execute(
        update(UserSession)
        .where(UserSession.id == session_id)
        .values(last_seen_at=datetime.now(UTC))
    )


# --------------------------------------------------------------------------
# Brute-force protection
# --------------------------------------------------------------------------
async def record_login_attempt(
    db: AsyncSession,
    *,
    email: str,
    user_id: uuid.UUID | None,
    successful: bool,
    failure_reason: str | None,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    db.add(
        LoginAttempt(
            email=email[:320],
            user_id=user_id,
            successful=successful,
            failure_reason=failure_reason,
            ip_address=ip_address,
            user_agent=(user_agent or "")[:400] or None,
            attempted_at=datetime.now(UTC),
        )
    )


def lockout_remaining_seconds(user: User) -> int:
    if user.locked_until is None:
        return 0
    remaining = (user.locked_until - datetime.now(UTC)).total_seconds()
    return max(0, int(remaining))


def register_failed_login(user: User) -> bool:
    """Increment the failure counter and lock the account when the limit is hit.

    Returns True when this failure caused a lock.
    """
    settings = get_settings()
    user.failed_login_count = (user.failed_login_count or 0) + 1
    if user.failed_login_count >= settings.login_max_attempts:
        user.locked_until = datetime.now(UTC) + timedelta(seconds=settings.login_lockout_seconds)
        user.failed_login_count = 0
        return True
    return False


def clear_failed_logins(user: User) -> None:
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = datetime.now(UTC)
