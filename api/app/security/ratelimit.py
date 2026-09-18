"""Rate limiting.

A fixed-window counter in Redis, incremented atomically. Fixed windows are used
rather than a sliding log because the counters are cheap, the behaviour is easy to
explain in an error message, and burst tolerance at a window edge is acceptable for
the limits involved here.

Sensitive endpoints (sign-in, password reset, public complaint submission) fail
closed when Redis is unavailable: refusing the request is better than silently
dropping brute-force protection. Ordinary endpoints fail open so that a Redis
outage does not take the whole workspace down.
"""

from __future__ import annotations

from dataclasses import dataclass

import redis.asyncio as redis
from redis.exceptions import RedisError

from ..config import get_settings
from ..errors import RateLimitedError, ServiceUnavailableError

_client: redis.Redis | None = None


@dataclass(frozen=True)
class Limit:
    """A named limit: ``max_requests`` per ``window_seconds``."""

    name: str
    max_requests: int
    window_seconds: int
    #: When true, a Redis failure blocks the request instead of allowing it.
    fail_closed: bool = False

    def key(self, identity: str) -> str:
        return f"maanak:rl:{self.name}:{identity}"


# Sign-in: per-IP ceiling, complementing the per-account lockout in the database.
LOGIN_PER_IP = Limit("login_ip", max_requests=20, window_seconds=300, fail_closed=True)
LOGIN_PER_ACCOUNT = Limit("login_account", max_requests=10, window_seconds=300, fail_closed=True)
REFRESH_PER_IP = Limit("refresh_ip", max_requests=120, window_seconds=300)
PASSWORD_RESET_PER_IP = Limit("pwreset_ip", max_requests=5, window_seconds=900, fail_closed=True)
COMPLAINT_PER_IP = Limit("complaint_ip", max_requests=5, window_seconds=3600, fail_closed=True)
COMPLAINT_STATUS_PER_IP = Limit("cstatus_ip", max_requests=30, window_seconds=600, fail_closed=True)
EVIDENCE_UPLOAD_PER_USER = Limit("upload_user", max_requests=120, window_seconds=600)
LISTING_FETCH_PER_USER = Limit("listing_user", max_requests=20, window_seconds=600)
REPORT_VERIFY_PER_IP = Limit("verify_ip", max_requests=60, window_seconds=600)
API_PER_SESSION = Limit("api_session", max_requests=600, window_seconds=60)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after_seconds: int
    limit: Limit


def get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(
            get_settings().redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def check(limit: Limit, identity: str) -> Decision:
    """Increment and evaluate a counter without raising."""
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return Decision(True, limit.max_requests, 0, limit)

    key = limit.key(identity)
    try:
        client = get_client()
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key, 1)
            pipe.ttl(key)
            count_raw, ttl_raw = await pipe.execute()
        count = int(count_raw)
        ttl = int(ttl_raw)
        if count == 1 or ttl < 0:
            await client.expire(key, limit.window_seconds)
            ttl = limit.window_seconds
    except RedisError:
        if limit.fail_closed:
            raise ServiceUnavailableError(
                "Request throttling is unavailable, so this request was refused.",
                code="rate_limit_backend_unavailable",
            ) from None
        return Decision(True, limit.max_requests, 0, limit)

    remaining = max(0, limit.max_requests - count)
    retry_after = max(1, ttl) if count > limit.max_requests else 0
    return Decision(count <= limit.max_requests, remaining, retry_after, limit)


async def enforce(limit: Limit, identity: str) -> None:
    """Raise ``RateLimitedError`` when the caller is over the limit."""
    decision = await check(limit, identity)
    if not decision.allowed:
        raise RateLimitedError(
            "Too many attempts. Wait before trying again.",
            details={
                "limit": limit.max_requests,
                "window_seconds": limit.window_seconds,
                "retry_after_seconds": decision.retry_after_seconds,
            },
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )


async def reset(limit: Limit, identity: str) -> None:
    """Clear a counter, used after a successful sign-in."""
    try:
        await get_client().delete(limit.key(identity))
    except RedisError:
        return


async def ping() -> bool:
    try:
        return bool(await get_client().ping())
    except RedisError:
        return False
