"""Worker entry point.

Started by compose as::

    arq app.worker.main.WorkerSettings

Concurrency is deliberately low. OCR is CPU-bound, so running many jobs at once on a
small host makes every job slower without improving throughput; ``max_jobs`` is set
to leave headroom for the API on the same machine.
"""

from __future__ import annotations

from typing import Any, ClassVar

from arq import cron

from ..config import get_settings
from ..observability import configure_logging, get_logger
from ..services.jobs import (
    JOB_ANALYSE_EVIDENCE,
    JOB_EVALUATE_RULES,
    redis_settings,
    requeue_stalled,
)
from .tasks import analyse_evidence, evaluate_inspection_rules

logger = get_logger(__name__)

# arq matches a task to a queued message by function name.
analyse_evidence.__name__ = JOB_ANALYSE_EVIDENCE
evaluate_inspection_rules.__name__ = JOB_EVALUATE_RULES


async def startup(context: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(json_output=settings.environment != "development")

    from ..services.storage import ensure_buckets

    try:
        await ensure_buckets()
    except Exception as exc:  # noqa: BLE001 - the worker retries per job
        logger.warning("worker_storage_not_ready", error=type(exc).__name__)

    from ..services.ocr import engine_status

    status = engine_status()
    logger.info(
        "worker_started",
        environment=settings.environment,
        ocr_engine=status.get("engine"),
        ocr_version=status.get("version"),
        ocr_languages=status.get("languages"),
        missing_languages=status.get("missing_languages"),
    )
    if status.get("missing_languages"):
        logger.error("worker_missing_ocr_languages", missing=status.get("missing_languages"))


async def shutdown(context: dict[str, Any]) -> None:
    from ..db import dispose_engines
    from ..services.jobs import close_pool

    await close_pool()
    await dispose_engines()
    logger.info("worker_stopped")


async def recover_stalled_jobs(context: dict[str, Any]) -> None:
    """Cron task: re-enqueue work abandoned by a worker that died mid-job."""
    from ..db import session_scope

    async with session_scope() as db:
        recovered = await requeue_stalled(db)
    if recovered:
        logger.info("stalled_jobs_recovered", count=recovered)


class WorkerSettings:
    """arq configuration."""

    functions: ClassVar[list[Any]] = [analyse_evidence, evaluate_inspection_rules]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()

    # OCR is CPU-bound; two concurrent jobs keeps a small host responsive.
    max_jobs = 2
    job_timeout = get_settings().job_timeout_seconds
    max_tries = get_settings().job_max_tries
    # Completed job metadata is kept in PostgreSQL, so Redis need not hold it long.
    keep_result = 300
    health_check_interval = 30
    health_check_key = "maanak:worker:health"

    cron_jobs: ClassVar[list[Any]] = [
        # arq declares its own coroutine protocol for cron targets, which an ordinary
        # async function does not structurally satisfy.
        cron(
            recover_stalled_jobs,  # type: ignore[arg-type]
            minute={5, 20, 35, 50},
            run_at_startup=False,
        ),
    ]
