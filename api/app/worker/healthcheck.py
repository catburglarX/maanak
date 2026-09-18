"""Worker health check.

Run by the container health check. Exits 0 only when the worker has written a
recent heartbeat to Redis and the OCR engine has its language data, so a worker that
is running but cannot actually do its job is reported unhealthy.
"""

from __future__ import annotations

import asyncio
import sys

from redis.exceptions import RedisError

HEALTH_KEY = "maanak:worker:health"
#: arq refreshes the health key on its own interval; allow a generous multiple.
MAX_HEARTBEAT_AGE_SECONDS = 120


async def check() -> tuple[bool, str]:
    from ..security.ratelimit import close_client, get_client
    from ..services.ocr import engine_status

    status = engine_status()
    if not status.get("available"):
        return False, "the OCR engine is not available"
    if status.get("missing_languages"):
        return False, f"OCR language data missing: {status['missing_languages']}"

    try:
        client = get_client()
        value = await client.get(HEALTH_KEY)
        if value is None:
            return False, "no worker heartbeat recorded"
        age = await client.ttl(HEALTH_KEY)
        if age is not None and age == -2:
            return False, "the worker heartbeat has expired"
    except RedisError as exc:
        return False, f"queue unreachable: {type(exc).__name__}"
    finally:
        await close_client()

    return True, "worker healthy"


def main() -> int:
    healthy, detail = asyncio.run(check())
    print(detail)  # noqa: T201 - the container health check reads stdout
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
