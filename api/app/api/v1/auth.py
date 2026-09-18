"""Authentication and account administration endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_settings
from ...db import get_db
from ...deps import (
    Principal,
    clear_session_cookies,
    get_principal,
    public_audit_context,
    requires,
    set_session_cookies,
)
from ...errors import NotFoundError, SessionExpiredError, ValidationError
from ...models.user import User
from ...schemas.auth import (
    AdminPasswordResetRequest,
    BootstrapRequest,
    LoginRequest,
    LoginResponse,
    PasswordChangeRequest,
    PasswordPolicyResponse,
    PasswordResetCompleteRequest,
    SessionSummary,
    UserCreateRequest,
    UserProfile,
    UserSummary,
    UserUpdateRequest,
)
from ...schemas.common import Acknowledgement, Page, PageNumber, PageSize
from ...security import Permission, tokens
from ...security import sessions as session_service
from ...security.ratelimit import (
    LOGIN_PER_ACCOUNT,
    LOGIN_PER_IP,
    PASSWORD_RESET_PER_IP,
    REFRESH_PER_IP,
    enforce,
)
from ...security.ratelimit import (
    reset as reset_limit,
)
from ...services import accounts
from ...services.phrasing import counted, verb

router = APIRouter(prefix="/auth", tags=["authentication"])
admin_router = APIRouter(prefix="/users", tags=["accounts"])


def _profile(principal: Principal) -> UserProfile:
    return UserProfile.model_validate(principal.public_profile())


def _user_summary(account: User) -> UserSummary:
    return UserSummary(
        id=str(account.id),
        email=account.email,
        name=account.name,
        role=account.role,
        designation=account.designation,
        jurisdiction_code=account.jurisdiction_code,
        jurisdiction_name=account.jurisdiction_name,
        is_active=account.is_active,
        last_login_at=account.last_login_at,
        locked=session_service.lockout_remaining_seconds(account) > 0,
        created_at=account.created_at,
    )


@router.get("/status", summary="Whether the workspace has been initialised")
async def workspace_status(db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    """Public. Lets the sign-in page offer bootstrap when no account exists yet."""
    return {"initialised": await accounts.workspace_is_initialised(db)}


@router.get("/password-policy", response_model=PasswordPolicyResponse)
async def password_policy() -> PasswordPolicyResponse:
    return PasswordPolicyResponse(
        min_length=get_settings().password_min_length,
        requirements=accounts.password_requirements(),
    )


@router.post(
    "/bootstrap",
    status_code=status.HTTP_201_CREATED,
    response_model=UserProfile,
    summary="Create the first administrator (once only)",
)
async def bootstrap(
    payload: BootstrapRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserProfile:
    context = public_audit_context(request)
    administrator = await accounts.bootstrap_administrator(
        db,
        context,
        email=str(payload.email),
        name=payload.name,
        password=payload.password,
        jurisdiction_code=payload.jurisdiction_code,
        jurisdiction_name=payload.jurisdiction_name,
        designation=payload.designation,
    )
    await db.commit()

    from ...security.permissions import JurisdictionScope

    principal = Principal(
        user=administrator,
        session_id=uuid.uuid4(),
        scope=JurisdictionScope.for_account(administrator.role, administrator.jurisdiction_code),
    )
    return _profile(principal)


@router.post("/sign-in", response_model=LoginResponse, summary="Sign in")
async def sign_in(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    context = public_audit_context(request)
    identity = context.ip_address or "unknown"
    await enforce(LOGIN_PER_IP, identity)
    await enforce(LOGIN_PER_ACCOUNT, str(payload.email).lower())

    try:
        result = await accounts.sign_in(
            db, context, email=str(payload.email), password=payload.password
        )
    except Exception:
        # The failure was recorded inside the service; persist that record.
        await db.commit()
        raise

    await db.commit()
    await reset_limit(LOGIN_PER_ACCOUNT, str(payload.email).lower())

    set_session_cookies(
        response,
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        csrf_token=result.csrf_token,
    )

    from ...security.permissions import JurisdictionScope

    principal = Principal(
        user=result.user,
        session_id=result.session.id,
        scope=JurisdictionScope.for_account(result.user.role, result.user.jurisdiction_code),
    )
    return LoginResponse(
        user=_profile(principal),
        access_expires_at=result.access_expires_at,
        csrf_token=result.csrf_token,
    )


@router.post("/refresh", response_model=LoginResponse, summary="Rotate the session")
async def refresh_session(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    context = public_audit_context(request)
    await enforce(REFRESH_PER_IP, context.ip_address or "unknown")

    raw_token = request.cookies.get(tokens.COOKIE_REFRESH)
    if not raw_token:
        raise SessionExpiredError

    try:
        result = await accounts.refresh(db, context, refresh_token=raw_token)
    except Exception:
        # Reuse detection revokes sessions; that must be persisted.
        await db.commit()
        clear_session_cookies(response)
        raise

    await db.commit()
    set_session_cookies(
        response,
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        csrf_token=result.csrf_token,
    )

    from ...security.permissions import JurisdictionScope

    principal = Principal(
        user=result.user,
        session_id=result.session.id,
        scope=JurisdictionScope.for_account(result.user.role, result.user.jurisdiction_code),
    )
    return LoginResponse(
        user=_profile(principal),
        access_expires_at=result.access_expires_at,
        csrf_token=result.csrf_token,
    )


@router.post("/sign-out", response_model=Acknowledgement, summary="Sign out")
async def sign_out(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> Acknowledgement:
    """Always succeeds, so it cannot be used to probe token validity."""
    context = public_audit_context(request)
    await accounts.sign_out(
        db,
        context,
        refresh_token=request.cookies.get(tokens.COOKIE_REFRESH),
        session_id=None,
    )
    await db.commit()
    clear_session_cookies(response)
    return Acknowledgement(message="Signed out.")


@router.get("/me", response_model=UserProfile, summary="The signed-in account")
async def whoami(principal: Principal = Depends(get_principal)) -> UserProfile:
    return _profile(principal)


@router.get(
    "/me/sessions",
    response_model=list[SessionSummary],
    summary="Active sessions for the signed-in account",
)
async def my_sessions(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> list[SessionSummary]:
    rows = await session_service.list_active_sessions(db, user_id=principal.id)
    return [
        SessionSummary(
            id=str(row.id),
            issued_at=row.issued_at,
            last_seen_at=row.last_seen_at,
            expires_at=row.expires_at,
            ip_address=str(row.ip_address) if row.ip_address else None,
            user_agent=row.user_agent,
            is_current=row.id == principal.session_id,
        )
        for row in rows
    ]


@router.delete(
    "/me/sessions/{session_id}",
    response_model=Acknowledgement,
    summary="End one of my sessions",
)
async def revoke_my_session(
    session_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Acknowledgement:
    from ...models.user import UserSession

    target = await db.get(UserSession, session_id)
    if target is None or target.user_id != principal.id:
        raise NotFoundError("That session was not found.")

    await session_service.revoke_family(
        db, family_id=target.family_id, reason=session_service.REVOKED_BY_USER
    )
    from ...services import audit as audit_service

    await audit_service.record(
        db,
        principal.audit_context(request),
        action="session.revoked",
        entity_type="user_session",
        entity_id=target.id,
    )
    await db.commit()
    return Acknowledgement(message="Session ended.")


@router.post(
    "/me/password",
    response_model=Acknowledgement,
    summary="Change my password",
)
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Acknowledgement:
    revoked = await accounts.change_own_password(
        db,
        principal.audit_context(request),
        account=principal.user,
        current_password=payload.current_password,
        new_password=payload.new_password,
        current_session_id=principal.session_id,
    )
    await db.commit()
    return Acknowledgement(
        message=(
            "Password changed."
            + (
                f" {counted(revoked, 'other session')} "
                f"{verb(revoked, 'was', 'were')} signed out."
                if revoked
                else ""
            )
        )
    )


@router.post(
    "/password-reset/complete",
    response_model=Acknowledgement,
    summary="Complete a password reset with a token",
)
async def complete_reset(
    payload: PasswordResetCompleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Acknowledgement:
    context = public_audit_context(request)
    await enforce(PASSWORD_RESET_PER_IP, context.ip_address or "unknown")
    await accounts.complete_password_reset(
        db, context, token=payload.token, new_password=payload.new_password
    )
    await db.commit()
    return Acknowledgement(message="Password set. Sign in with your new password.")


# --------------------------------------------------------------------------
# Account administration
# --------------------------------------------------------------------------
@admin_router.get("", response_model=Page[UserSummary], summary="List accounts")
async def list_users(
    page: PageNumber = Query(1),
    page_size: PageSize = Query(25),
    search: str = Query("", max_length=120),
    role: str | None = Query(None),
    active: bool | None = Query(None),
    principal: Principal = Depends(requires(Permission.USER_READ)),
    db: AsyncSession = Depends(get_db),
) -> Page[UserSummary]:
    conditions: list[Any] = []
    if search:
        pattern = f"%{search.lower()}%"
        conditions.append(
            func.lower(User.name).like(pattern) | func.lower(User.email).like(pattern)
        )
    if role:
        conditions.append(User.role == role)
    if active is not None:
        conditions.append(User.is_active.is_(active))

    # A controller may only see accounts inside its own jurisdiction subtree.
    scope_filter = principal.scope.filter(User.jurisdiction_code)
    if scope_filter is not None:
        conditions.append(scope_filter)

    total = await db.scalar(select(func.count()).select_from(User).where(*conditions)) or 0
    rows = await db.scalars(
        select(User)
        .where(*conditions)
        .order_by(User.name.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return Page.build(
        [_user_summary(row) for row in rows], page=page, page_size=page_size, total=int(total)
    )


@admin_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=UserSummary,
    summary="Create an account",
)
async def create_user(
    payload: UserCreateRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.USER_CREATE)),
    db: AsyncSession = Depends(get_db),
) -> UserSummary:
    account = await accounts.create_user(
        db,
        principal.audit_context(request),
        email=str(payload.email),
        name=payload.name,
        password=payload.password,
        role=payload.role,
        jurisdiction_code=payload.jurisdiction_code,
        jurisdiction_name=payload.jurisdiction_name,
        designation=payload.designation,
        must_change_password=payload.must_change_password,
    )
    await db.commit()
    return _user_summary(account)


@admin_router.get("/{user_id}", response_model=UserSummary, summary="Read one account")
async def read_user(
    user_id: uuid.UUID,
    principal: Principal = Depends(requires(Permission.USER_READ)),
    db: AsyncSession = Depends(get_db),
) -> UserSummary:
    account = await accounts.get_account_or_404(db, user_id)
    principal.scope.require(account.jurisdiction_code)
    return _user_summary(account)


@admin_router.patch("/{user_id}", response_model=UserSummary, summary="Update an account")
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.USER_UPDATE)),
    db: AsyncSession = Depends(get_db),
) -> UserSummary:
    account = await accounts.get_account_or_404(db, user_id)
    principal.scope.require(account.jurisdiction_code)

    changes = payload.model_dump(exclude_unset=True, exclude={"reason"})
    if payload.jurisdiction_code is not None:
        # An administrator may move an account anywhere; a controller may not move
        # an account out of its own subtree.
        principal.scope.require(payload.jurisdiction_code)

    account = await accounts.update_user(
        db,
        principal.audit_context(request),
        account=account,
        changes=changes,
        reason=payload.reason,
        acting_user_id=principal.id,
    )
    await db.commit()
    return _user_summary(account)


@admin_router.get(
    "/{user_id}/sessions",
    response_model=list[SessionSummary],
    summary="Active sessions for an account",
)
async def user_sessions(
    user_id: uuid.UUID,
    principal: Principal = Depends(requires(Permission.SESSION_REVOKE_ANY)),
    db: AsyncSession = Depends(get_db),
) -> list[SessionSummary]:
    account = await accounts.get_account_or_404(db, user_id)
    principal.scope.require(account.jurisdiction_code)
    rows = await session_service.list_active_sessions(db, user_id=account.id)
    return [
        SessionSummary(
            id=str(row.id),
            issued_at=row.issued_at,
            last_seen_at=row.last_seen_at,
            expires_at=row.expires_at,
            ip_address=str(row.ip_address) if row.ip_address else None,
            user_agent=row.user_agent,
            is_current=False,
        )
        for row in rows
    ]


@admin_router.delete(
    "/{user_id}/sessions",
    response_model=Acknowledgement,
    summary="End every session for an account",
)
async def revoke_user_sessions(
    user_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(requires(Permission.SESSION_REVOKE_ANY)),
    db: AsyncSession = Depends(get_db),
) -> Acknowledgement:
    account = await accounts.get_account_or_404(db, user_id)
    principal.scope.require(account.jurisdiction_code)

    revoked = await session_service.revoke_all_for_user(
        db, user_id=account.id, reason=session_service.REVOKED_BY_ADMIN
    )
    from ...services import audit as audit_service

    await audit_service.record(
        db,
        principal.audit_context(request),
        action="session.revoked_by_administrator",
        entity_type="user",
        entity_id=account.id,
        new_values={"sessions_revoked": revoked},
    )
    await db.commit()
    return Acknowledgement(message=f"{counted(revoked, 'session')} ended.")


@admin_router.post(
    "/{user_id}/password-reset",
    summary="Issue a password reset token for an account",
)
async def issue_reset(
    user_id: uuid.UUID,
    payload: AdminPasswordResetRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.USER_RESET_PASSWORD)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Returns the token once.

    With SMTP configured this would be emailed to the account holder instead. The
    token is shown here so the flow is usable in a local deployment without an
    email service, and it is recorded in the audit trail either way.
    """
    account = await accounts.get_account_or_404(db, user_id)
    principal.scope.require(account.jurisdiction_code)
    if account.id == principal.id:
        raise ValidationError(
            "Use the change-password form for your own account.",
            code="self_reset_not_allowed",
        )

    token = await accounts.issue_password_reset(
        db,
        principal.audit_context(request),
        account=account,
        issued_by_id=principal.id,
        reason=payload.reason,
    )
    await db.commit()
    return {
        "token": token,
        "expires_in_seconds": accounts.PASSWORD_RESET_TTL_SECONDS,
        "expires_at": datetime.now(UTC),
        "delivery": "returned_once_no_email_configured",
        "note": "Give this to the account holder over a trusted channel. It can be used once.",
    }
