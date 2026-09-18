"""Accounts, sign-in and session management.

The rules that matter here:

* bootstrap is guarded by an advisory lock plus a re-check, so two concurrent
  calls cannot both create a first administrator;
* sign-in returns one generic failure for every reason, and spends comparable time
  whether or not the account exists;
* refresh rotates the token and treats reuse of a rotated token as a compromise,
  revoking the whole family;
* changing a password, role, jurisdiction or active flag revokes existing
  sessions, so a demoted account cannot keep acting with stale claims.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..domain.enums import Role
from ..errors import (
    AccountLockedError,
    ConflictError,
    InvalidCredentialsError,
    NotFoundError,
    PermissionDeniedError,
    SessionExpiredError,
    ValidationError,
)
from ..models.user import PasswordResetToken, User, UserSession
from ..observability import LOGIN_ATTEMPTS, get_logger
from ..security import passwords, sessions, tokens
from ..security.permissions import normalise_jurisdiction
from . import audit as audit_service

logger = get_logger(__name__)

BOOTSTRAP_LOCK_KEY = "maanak_bootstrap"
PASSWORD_RESET_TTL_SECONDS = 3600


@dataclass
class SignInResult:
    user: User
    session: UserSession
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    csrf_token: str


def _normalise_email(email: str) -> str:
    return email.strip().lower()


async def workspace_is_initialised(db: AsyncSession) -> bool:
    return bool(await db.scalar(select(func.count()).select_from(User)))


async def bootstrap_administrator(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    email: str,
    name: str,
    password: str,
    jurisdiction_code: str,
    jurisdiction_name: str,
    designation: str | None,
) -> User:
    """Create the first administrator. Permitted exactly once."""
    # Serialise concurrent bootstrap attempts for the rest of this transaction.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": BOOTSTRAP_LOCK_KEY}
    )
    if await workspace_is_initialised(db):
        raise ConflictError("Initial setup has already been completed.", code="already_initialised")

    address = _normalise_email(email)
    policy = passwords.check_policy(password, email=address, name=name)
    if not policy.acceptable:
        raise ValidationError(policy.message, details={"field": "password"})

    administrator = User(
        id=uuid.uuid4(),
        email=address,
        name=name.strip(),
        password_hash=passwords.hash_password(password),
        password_changed_at=datetime.now(UTC),
        must_change_password=False,
        role=Role.ADMIN,
        jurisdiction_code=normalise_jurisdiction(jurisdiction_code),
        jurisdiction_name=jurisdiction_name.strip(),
        designation=designation,
        is_active=True,
    )
    db.add(administrator)
    await db.flush()

    await audit_service.record(
        db,
        audit_service.AuditContext(
            actor_id=administrator.id,
            actor_role=administrator.role,
            actor_email=administrator.email,
            jurisdiction_code=administrator.jurisdiction_code,
            request_id=context.request_id,
            ip_address=context.ip_address,
            user_agent=context.user_agent,
        ),
        action="workspace.bootstrapped",
        entity_type="user",
        entity_id=administrator.id,
        new_values={
            "email": administrator.email,
            "role": administrator.role,
            "jurisdiction_code": administrator.jurisdiction_code,
        },
        reason="One-time workspace initialisation.",
    )
    return administrator


async def sign_in(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    email: str,
    password: str,
) -> SignInResult:
    """Authenticate and open a session.

    Every failure path raises the same error with the same message. The only
    exception is a locked account, which must tell the user to wait.
    """
    address = _normalise_email(email)
    user = await db.scalar(select(User).where(func.lower(User.email) == address))

    if user is None:
        # Equalise timing so a missing account is not measurably faster.
        passwords.dummy_verify()
        await sessions.record_login_attempt(
            db,
            email=address,
            user_id=None,
            successful=False,
            failure_reason="unknown_account",
            ip_address=context.ip_address,
            user_agent=context.user_agent,
        )
        LOGIN_ATTEMPTS.labels(outcome="unknown_account").inc()
        await _audit_login_failure(db, context, address, "unknown_account")
        raise InvalidCredentialsError

    remaining = sessions.lockout_remaining_seconds(user)
    if remaining > 0:
        await sessions.record_login_attempt(
            db,
            email=address,
            user_id=user.id,
            successful=False,
            failure_reason="locked",
            ip_address=context.ip_address,
            user_agent=context.user_agent,
        )
        LOGIN_ATTEMPTS.labels(outcome="locked").inc()
        await _audit_login_failure(db, context, address, "locked")
        raise AccountLockedError(
            "Too many sign-in attempts. Try again later.",
            details={"retry_after_seconds": remaining},
            headers={"Retry-After": str(remaining)},
        )

    matched, needs_rehash = passwords.verify_password(password, user.password_hash)

    if not matched or not user.is_active:
        reason = "wrong_password" if user.is_active else "inactive_account"
        locked = sessions.register_failed_login(user) if user.is_active else False
        await sessions.record_login_attempt(
            db,
            email=address,
            user_id=user.id,
            successful=False,
            failure_reason=reason,
            ip_address=context.ip_address,
            user_agent=context.user_agent,
        )
        LOGIN_ATTEMPTS.labels(outcome=reason).inc()
        await _audit_login_failure(db, context, address, reason, locked=locked)
        # Identical error for a wrong password and a disabled account.
        raise InvalidCredentialsError

    if needs_rehash:
        # Transparently upgrade a hash created under weaker parameters.
        user.password_hash = passwords.hash_password(password)

    sessions.clear_failed_logins(user)
    issued = await sessions.create_session(
        db, user=user, ip_address=context.ip_address, user_agent=context.user_agent
    )
    access_token, access_expires_at = tokens.issue_access_token(
        user_id=user.id,
        session_id=issued.session.id,
        role=user.role,
        jurisdiction_code=user.jurisdiction_code,
    )
    csrf_token = tokens.generate_csrf_token()

    await sessions.record_login_attempt(
        db,
        email=address,
        user_id=user.id,
        successful=True,
        failure_reason=None,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
    )
    LOGIN_ATTEMPTS.labels(outcome="success").inc()
    await audit_service.record(
        db,
        audit_service.AuditContext(
            actor_id=user.id,
            actor_role=user.role,
            actor_email=user.email,
            jurisdiction_code=user.jurisdiction_code,
            request_id=context.request_id,
            ip_address=context.ip_address,
            user_agent=context.user_agent,
        ),
        action="auth.signed_in",
        entity_type="user",
        entity_id=user.id,
        new_values={"session_id": str(issued.session.id)},
    )

    return SignInResult(
        user=user,
        session=issued.session,
        access_token=access_token,
        access_expires_at=access_expires_at,
        refresh_token=issued.refresh_token,
        csrf_token=csrf_token,
    )


async def _audit_login_failure(
    db: AsyncSession,
    context: audit_service.AuditContext,
    email: str,
    reason: str,
    *,
    locked: bool = False,
) -> None:
    await audit_service.record(
        db,
        context,
        action="auth.sign_in_failed",
        entity_type="user",
        entity_id=None,
        new_values={"email": email, "reason": reason, "account_locked": locked},
    )


@dataclass
class RefreshResult:
    user: User
    session: UserSession
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    csrf_token: str


async def refresh(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    refresh_token: str,
) -> RefreshResult:
    """Rotate a refresh token, detecting reuse of an already-rotated token."""
    session = await sessions.find_session_by_refresh_token(db, refresh_token)
    if session is None:
        raise SessionExpiredError

    if session.revoked_at is not None:
        # This token was already exchanged. Either it leaked, or a client retried.
        # Both are handled the same way: end the whole family.
        revoked = await sessions.revoke_family(
            db, family_id=session.family_id, reason=sessions.REVOKED_REUSE_DETECTED
        )
        await audit_service.record(
            db,
            context,
            action="auth.refresh_reuse_detected",
            entity_type="user_session",
            entity_id=session.id,
            new_values={
                "family_id": str(session.family_id),
                "sessions_revoked": revoked,
            },
            reason="A rotated refresh token was presented again.",
        )
        logger.warning(
            "refresh_token_reuse_detected",
            family_id=str(session.family_id),
            sessions_revoked=revoked,
        )
        raise SessionExpiredError(
            "Your session ended for security reasons. Sign in again.",
            code="session_revoked",
        )

    if not sessions.session_is_usable(session):
        raise SessionExpiredError

    user = await db.get(User, session.user_id)
    if user is None or not user.is_active:
        await sessions.revoke_family(
            db, family_id=session.family_id, reason=sessions.REVOKED_ACCOUNT_DISABLED
        )
        raise SessionExpiredError

    rotated = await sessions.rotate_session(
        db, session=session, ip_address=context.ip_address, user_agent=context.user_agent
    )
    access_token, access_expires_at = tokens.issue_access_token(
        user_id=user.id,
        session_id=rotated.session.id,
        role=user.role,
        jurisdiction_code=user.jurisdiction_code,
    )
    return RefreshResult(
        user=user,
        session=rotated.session,
        access_token=access_token,
        access_expires_at=access_expires_at,
        refresh_token=rotated.refresh_token,
        csrf_token=tokens.generate_csrf_token(),
    )


async def sign_out(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    refresh_token: str | None,
    session_id: uuid.UUID | None,
) -> None:
    """End the caller's session. Never reports whether a token was valid."""
    target: UserSession | None = None
    if refresh_token:
        target = await sessions.find_session_by_refresh_token(db, refresh_token)
    if target is None and session_id is not None:
        target = await db.get(UserSession, session_id)
    if target is None:
        return

    await sessions.revoke_family(db, family_id=target.family_id, reason=sessions.REVOKED_BY_USER)
    await audit_service.record(
        db,
        context,
        action="auth.signed_out",
        entity_type="user_session",
        entity_id=target.id,
        new_values={"family_id": str(target.family_id)},
    )


# --------------------------------------------------------------------------
# Account administration
# --------------------------------------------------------------------------
async def create_user(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    email: str,
    name: str,
    password: str,
    role: Role,
    jurisdiction_code: str,
    jurisdiction_name: str,
    designation: str | None,
    must_change_password: bool,
) -> User:
    address = _normalise_email(email)
    policy = passwords.check_policy(password, email=address, name=name)
    if not policy.acceptable:
        raise ValidationError(policy.message, details={"field": "password"})

    account = User(
        id=uuid.uuid4(),
        email=address,
        name=name.strip(),
        password_hash=passwords.hash_password(password),
        password_changed_at=datetime.now(UTC),
        must_change_password=must_change_password,
        role=role,
        jurisdiction_code=normalise_jurisdiction(jurisdiction_code),
        jurisdiction_name=jurisdiction_name.strip(),
        designation=designation,
        is_active=True,
    )
    db.add(account)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(
            "An account with that email address already exists.",
            code="email_taken",
            details={"field": "email"},
        ) from exc

    await audit_service.record(
        db,
        context,
        action="user.created",
        entity_type="user",
        entity_id=account.id,
        new_values={
            "email": account.email,
            "name": account.name,
            "role": account.role,
            "jurisdiction_code": account.jurisdiction_code,
        },
    )
    return account


async def update_user(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    account: User,
    changes: dict[str, Any],
    reason: str | None,
    acting_user_id: uuid.UUID,
) -> User:
    """Apply administrative changes, revoking sessions when claims change."""
    if not changes:
        return account

    if account.id == acting_user_id:
        if changes.get("role") is not None and changes["role"] != account.role:
            raise PermissionDeniedError("You cannot change your own role.", code="self_role_change")
        if changes.get("is_active") is False:
            raise PermissionDeniedError(
                "You cannot deactivate your own account.", code="self_deactivate"
            )

    before = {
        "role": account.role,
        "jurisdiction_code": account.jurisdiction_code,
        "jurisdiction_name": account.jurisdiction_name,
        "name": account.name,
        "designation": account.designation,
        "is_active": account.is_active,
    }

    claims_changed = False
    for field, value in changes.items():
        if value is None:
            continue
        if field == "jurisdiction_code":
            value = normalise_jurisdiction(value)
        if field in {"role", "jurisdiction_code"} and getattr(account, field) != value:
            claims_changed = True
        if field == "is_active" and account.is_active and value is False:
            account.deactivated_at = datetime.now(UTC)
            account.deactivated_reason = reason
            claims_changed = True
        if field == "is_active" and value is True:
            account.deactivated_at = None
            account.deactivated_reason = None
            account.locked_until = None
            account.failed_login_count = 0
        setattr(account, field, value)

    await db.flush()

    revoked = 0
    if claims_changed:
        revoked = await sessions.revoke_all_for_user(
            db,
            user_id=account.id,
            reason=(
                sessions.REVOKED_ACCOUNT_DISABLED
                if not account.is_active
                else sessions.REVOKED_ROLE_CHANGE
            ),
        )

    after = {
        "role": account.role,
        "jurisdiction_code": account.jurisdiction_code,
        "jurisdiction_name": account.jurisdiction_name,
        "name": account.name,
        "designation": account.designation,
        "is_active": account.is_active,
    }
    changed_fields = {key: after[key] for key in after if after[key] != before[key]}

    await audit_service.record(
        db,
        context,
        action="user.updated",
        entity_type="user",
        entity_id=account.id,
        old_values={key: before[key] for key in changed_fields},
        new_values={**changed_fields, "sessions_revoked": revoked},
        reason=reason,
    )
    return account


async def change_own_password(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    account: User,
    current_password: str,
    new_password: str,
    current_session_id: uuid.UUID,
) -> int:
    """Change a password and end every other session. Returns sessions revoked."""
    matched, _ = passwords.verify_password(current_password, account.password_hash)
    if not matched:
        raise InvalidCredentialsError(
            "Your current password is incorrect.", code="current_password_invalid"
        )

    policy = passwords.check_policy(new_password, email=account.email, name=account.name)
    if not policy.acceptable:
        raise ValidationError(policy.message, details={"field": "new_password"})

    reused, _ = passwords.verify_password(new_password, account.password_hash)
    if reused:
        raise ValidationError(
            "Choose a password you have not used here before.",
            details={"field": "new_password"},
        )

    account.password_hash = passwords.hash_password(new_password)
    account.password_changed_at = datetime.now(UTC)
    account.must_change_password = False
    await db.flush()

    revoked = 0
    for session in await sessions.list_active_sessions(db, user_id=account.id):
        if session.id != current_session_id:
            await sessions.revoke_family(
                db,
                family_id=session.family_id,
                reason=sessions.REVOKED_PASSWORD_CHANGE,
            )
            revoked += 1

    await audit_service.record(
        db,
        context,
        action="auth.password_changed",
        entity_type="user",
        entity_id=account.id,
        new_values={"other_sessions_revoked": revoked},
    )
    return revoked


async def issue_password_reset(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    account: User,
    issued_by_id: uuid.UUID | None,
    reason: str | None,
) -> str:
    """Create a single-use reset token and return its plaintext form.

    The plaintext is returned to the caller once; only its hash is stored. In a
    deployment with email configured this would be delivered to the account
    holder instead of shown to the administrator.
    """
    now = datetime.now(UTC)
    plaintext = tokens.generate_refresh_token()
    db.add(
        PasswordResetToken(
            id=uuid.uuid4(),
            user_id=account.id,
            token_hash=tokens.hash_token(plaintext),
            issued_at=now,
            expires_at=now + timedelta(seconds=PASSWORD_RESET_TTL_SECONDS),
            issued_by_id=issued_by_id,
            request_metadata={"reason": reason} if reason else {},
        )
    )
    account.must_change_password = True
    await db.flush()

    await audit_service.record(
        db,
        context,
        action="user.password_reset_issued",
        entity_type="user",
        entity_id=account.id,
        reason=reason,
    )
    return plaintext


async def complete_password_reset(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    token: str,
    new_password: str,
) -> User:
    now = datetime.now(UTC)
    record = await db.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == tokens.hash_token(token))
    )
    if record is None or record.used_at is not None or record.expires_at <= now:
        raise ValidationError(
            "That reset link is no longer valid. Request a new one.",
            code="reset_token_invalid",
        )

    account = await db.get(User, record.user_id)
    if account is None or not account.is_active:
        raise ValidationError(
            "That reset link is no longer valid. Request a new one.",
            code="reset_token_invalid",
        )

    policy = passwords.check_policy(new_password, email=account.email, name=account.name)
    if not policy.acceptable:
        raise ValidationError(policy.message, details={"field": "new_password"})

    account.password_hash = passwords.hash_password(new_password)
    account.password_changed_at = now
    account.must_change_password = False
    account.failed_login_count = 0
    account.locked_until = None
    record.used_at = now
    await db.flush()

    revoked = await sessions.revoke_all_for_user(
        db, user_id=account.id, reason=sessions.REVOKED_PASSWORD_CHANGE
    )
    await audit_service.record(
        db,
        context,
        action="auth.password_reset_completed",
        entity_type="user",
        entity_id=account.id,
        new_values={"sessions_revoked": revoked},
    )
    return account


async def get_account_or_404(db: AsyncSession, user_id: uuid.UUID) -> User:
    account = await db.get(User, user_id)
    if account is None:
        raise NotFoundError("That account was not found.")
    return account


def password_requirements() -> list[str]:
    settings = get_settings()
    return [
        f"At least {settings.password_min_length} characters.",
        "Not a commonly used password.",
        "Does not contain your name or email address.",
        "No character repeated four or more times in a row.",
        "Mix letters with numbers or symbols, or use a passphrase of 16 or more characters.",
    ]
