"""Health, readiness and metrics endpoints.

``/health/live`` answers only "is this process running": no dependency calls, so a
slow database cannot cause a restart loop.

``/health/ready`` checks the dependencies this instance needs to serve traffic.
It reports component status but never connection strings, credentials, hostnames or
version details of internal services.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..config import get_settings
from ..db import get_db
from ..observability import get_logger
from ..security.ratelimit import ping as redis_ping

router = APIRouter(tags=["operations"])
logger = get_logger(__name__)

CHECK_TIMEOUT_SECONDS = 3.0


@router.get("/health/live", summary="Liveness")
async def live() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


async def _check_database(db: AsyncSession) -> tuple[bool, str]:
    try:
        await asyncio.wait_for(db.execute(text("SELECT 1")), CHECK_TIMEOUT_SECONDS)
    except (TimeoutError, Exception) as exc:
        logger.warning("readiness_database_failed", error=type(exc).__name__)
        return False, "unavailable"
    return True, "ok"


async def _check_queue() -> tuple[bool, str]:
    try:
        reachable = await asyncio.wait_for(redis_ping(), CHECK_TIMEOUT_SECONDS)
    except TimeoutError:
        return False, "timeout"
    return (True, "ok") if reachable else (False, "unavailable")


async def _check_storage() -> tuple[bool, str]:
    from ..services.storage import health as storage_health

    try:
        ok = await asyncio.wait_for(storage_health(), CHECK_TIMEOUT_SECONDS)
    except TimeoutError:
        return False, "timeout"
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        logger.warning("readiness_storage_failed", error=type(exc).__name__)
        return False, "unavailable"
    return (True, "ok") if ok else (False, "unavailable")


async def _check_ocr() -> tuple[bool, str]:
    from ..services.ocr import engine_status

    try:
        status_report = await asyncio.to_thread(engine_status)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        logger.warning("readiness_ocr_failed", error=type(exc).__name__)
        return False, "unavailable"
    if not status_report.get("available"):
        return False, "unavailable"
    if status_report.get("missing_languages"):
        return False, "missing_language_data"
    return True, "ok"


@router.get("/health/ready", summary="Readiness")
async def ready(response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    database_ok, database_detail = await _check_database(db)
    queue_ok, queue_detail = await _check_queue()
    storage_ok, storage_detail = await _check_storage()
    ocr_ok, ocr_detail = await _check_ocr()

    components = {
        "database": database_detail,
        "queue": queue_detail,
        "object_storage": storage_detail,
        "ocr": ocr_detail,
    }
    healthy = database_ok and queue_ok and storage_ok and ocr_ok
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if healthy else "degraded",
        "version": __version__,
        "components": components,
    }


@router.get("/health", summary="Readiness (alias)")
async def health(response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await ready(response, db)


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus exposition.

    Not exposed publicly by the nginx configuration; reachable inside the
    deployment network only.
    """
    if get_settings().is_production and not get_settings().docs_enabled:
        # Metrics stay available in production; this branch only documents that
        # the endpoint is intentionally unauthenticated behind the proxy.
        pass
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
