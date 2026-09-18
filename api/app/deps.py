"""Request dependencies.

Browser sessions use cookies, not an ``Authorization`` header:

* the access token is in an ``HttpOnly`` cookie, so script injected into the page
  cannot read it;
* unsafe methods additionally require a CSRF header matching a readable cookie
  (double-submit), which script from another origin cannot set;
* ``SameSite=Lax`` blocks the common cross-site form-post case.

A bearer header is deliberately not accepted. Storing a token where JavaScript can
read it trades an XSS-readable credential for convenience, and this application
handles evidence that must not be exfiltratable by a single injected script.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_db
from .domain.enums import Role
from .errors import AuthenticationError, PermissionDeniedError, SessionExpiredError
from .models.user import User, UserSession
from .observability import current_request_id, set_actor
from .security import permissions as perms
from .security import tokens
from .security.sessions import session_is_usable
from .services.audit import AuditContext

CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Principal:
    """The authenticated caller and everything derived from the session."""

    user: User
    session_id: uuid.UUID
    scope: perms.JurisdictionScope

    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    @property
    def role(self) -> Role:
        return Role(self.user.role)

    @property
    def email(self) -> str:
        return self.user.email

    @property
    def jurisdiction_code(self) -> str:
        return self.user.jurisdiction_code

    def can(self, permission: perms.Permission) -> bool:
        return perms.has_permission(self.user.role, permission)

    def require(self, permission: perms.Permission) -> None:
        perms.require_permission(self.user.role, permission)

    def audit_context(self, request: Request) -> AuditContext:
        return AuditContext(
            actor_id=self.user.id,
            actor_role=self.user.role,
            actor_email=self.user.email,
            jurisdiction_code=self.user.jurisdiction_code,
            request_id=current_request_id(),
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )

    def public_profile(self) -> dict[str, object]:
        return {
            "id": str(self.user.id),
            "email": self.user.email,
            "name": self.user.name,
            "role": self.user.role,
            "designation": self.user.designation,
            "jurisdiction_code": self.user.jurisdiction_code,
            "jurisdiction_name": self.user.jurisdiction_name,
            "scope": self.scope.describe(),
            "must_change_password": self.user.must_change_password,
            "permissions": sorted(
                permission.value for permission in perms.permissions_for(self.user.role)
            ),
        }


def client_ip(request: Request) -> str | None:
    """Client address, honouring one layer of reverse proxy.

    uvicorn is started with ``--proxy-headers``, so ``request.client.host`` is
    already the forwarded address when a trusted proxy set it. The raw header is
    read only as a fallback and only its first entry is used.
    """
    if request.client and request.client.host:
        return request.client.host
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return None


def public_audit_context(request: Request) -> AuditContext:
    return AuditContext.public(
        request_id=current_request_id(),
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


def verify_csrf(request: Request) -> None:
    """Double-submit CSRF check for state-changing requests."""
    if request.method in SAFE_METHODS:
        return
    cookie_value = request.cookies.get(tokens.COOKIE_CSRF)
    header_value = request.headers.get(CSRF_HEADER)
    if not cookie_value or not header_value:
        raise PermissionDeniedError(
            "This request is missing its CSRF token. Reload the page and try again.",
            code="csrf_token_missing",
        )
    if not tokens.tokens_equal(header_value, cookie_value):
        raise PermissionDeniedError(
            "The CSRF token did not match. Reload the page and try again.",
            code="csrf_token_invalid",
        )


async def get_principal(request: Request, db: AsyncSession = Depends(get_db)) -> Principal:
    """Resolve the caller from the access cookie.

    The session row is checked on every request rather than trusting the token
    alone, so an administrator revoking a session takes effect immediately instead
    of at the end of the access token's lifetime.
    """
    raw_token = request.cookies.get(tokens.COOKIE_ACCESS)
    if not raw_token:
        raise AuthenticationError

    claims = tokens.decode_access_token(raw_token)

    session = await db.get(UserSession, claims.session_id)
    if session is None or not session_is_usable(session):
        raise SessionExpiredError

    user = await db.get(User, claims.user_id)
    if user is None or not user.is_active:
        raise SessionExpiredError

    # A role or jurisdiction change invalidates the token's claims. Refusing here
    # forces a refresh, which re-reads the account.
    if user.role != claims.role or user.jurisdiction_code != claims.jurisdiction_code:
        raise SessionExpiredError("Your permissions changed. Sign in again.", code="claims_stale")

    verify_csrf(request)
    set_actor(str(user.id))

    return Principal(
        user=user,
        session_id=session.id,
        scope=perms.JurisdictionScope.for_account(user.role, user.jurisdiction_code),
    )


def requires(
    *required: perms.Permission,
) -> Callable[[Principal], Awaitable[Principal]]:
    """Dependency factory enforcing that the caller holds every listed permission."""

    async def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        for permission in required:
            principal.require(permission)
        return principal

    return dependency


def requires_any(
    *accepted: perms.Permission,
) -> Callable[[Principal], Awaitable[Principal]]:
    """Dependency factory enforcing at least one of the listed permissions."""

    async def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if not any(principal.can(permission) for permission in accepted):
            raise PermissionDeniedError(
                "Your role does not permit this action.",
                details={"accepted_permissions": [item.value for item in accepted]},
            )
        return principal

    return dependency


def set_session_cookies(
    response, *, access_token: str, refresh_token: str, csrf_token: str
) -> None:
    """Attach session cookies with the strictest workable attributes."""
    settings = get_settings()
    common = {
        "domain": settings.cookie_domain,
        "secure": settings.cookie_secure,
        "samesite": settings.cookie_samesite,
        "path": "/",
    }
    response.set_cookie(
        tokens.COOKIE_ACCESS,
        access_token,
        max_age=settings.access_token_ttl_seconds,
        httponly=True,
        **common,
    )
    response.set_cookie(
        tokens.COOKIE_REFRESH,
        refresh_token,
        max_age=settings.refresh_token_ttl_seconds,
        httponly=True,
        # Restricted to the refresh and sign-out endpoints so the long-lived
        # credential is not attached to every ordinary request.
        **{**common, "path": "/api/v1/auth"},
    )
    response.set_cookie(
        tokens.COOKIE_CSRF,
        csrf_token,
        max_age=settings.refresh_token_ttl_seconds,
        # Readable by the application script on purpose: it must echo the value
        # back in the X-CSRF-Token header.
        httponly=False,
        **common,
    )


def clear_session_cookies(response) -> None:
    settings = get_settings()
    for name, path in (
        (tokens.COOKIE_ACCESS, "/"),
        (tokens.COOKIE_REFRESH, "/api/v1/auth"),
        (tokens.COOKIE_CSRF, "/"),
    ):
        response.delete_cookie(
            name,
            path=path,
            domain=settings.cookie_domain,
            secure=settings.cookie_secure,
            samesite=settings.cookie_samesite,
        )
