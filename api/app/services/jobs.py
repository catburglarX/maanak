"""Background job queue.

Two records exist for every piece of background work:

* a row in ``analysis_jobs``, which is the durable source of truth the UI polls;
* a message in Redis, which is what actually wakes a worker.

Keeping the database row authoritative means a Redis restart loses scheduling but
never loses the fact that work is outstanding: ``requeue_stalled`` finds those rows
and enqueues them again.

``idempotency_key`` is derived from the work itself, so enqueueing the same analysis
twice returns the existing job instead of creating duplicate candidates.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from redis.exceptions import RedisError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..domain.enums import AnalysisState, JobState
from ..models.evidence import AnalysisJob, Evidence
from ..observability import JOBS_ENQUEUED, QUEUE_DEPTH, get_logger

logger = get_logger(__name__)

JOB_ANALYSE_EVIDENCE = "analyse_evidence"
JOB_EVALUATE_RULES = "evaluate_inspection_rules"
JOB_GENERATE_REPORT = "generate_report_documents"

_pool: ArqRedis | None = None


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


async def get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings())
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


@dataclass
class EnqueuedJob:
    job: AnalysisJob
    created: bool
    queued_to_redis: bool


def evidence_analysis_key(evidence_id: uuid.UUID, *, attempt_tag: str = "v1") -> str:
    """Idempotency key for analysing one evidence file.

    Includes a tag so that a deliberate re-analysis (for example after an OCR
    upgrade) can be requested without colliding with the original job.
    """
    return f"{JOB_ANALYSE_EVIDENCE}:{evidence_id}:{attempt_tag}"


def rule_evaluation_key(inspection_id: uuid.UUID, *, revision: int) -> str:
    return f"{JOB_EVALUATE_RULES}:{inspection_id}:{revision}"


async def enqueue(
    db: AsyncSession,
    *,
    job_type: str,
    idempotency_key: str,
    evidence_id: uuid.UUID | None = None,
    inspection_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    requested_by_id: uuid.UUID | None = None,
) -> EnqueuedJob:
    """Create a job row and register it to be pushed to Redis after the commit.

    The push is deliberately **not** done here. ``flush()`` writes the row inside the
    open transaction, where no other connection can see it, so pushing at that point
    is a race: a worker can take the message, look for the row, find nothing and drop
    the work. That failure is silent and leaves the job sitting at ``queued`` forever.

    So the job id is recorded on the session and pushed by ``push_pending`` once the
    caller has committed. See ``get_db`` in ``app/db.py``.
    """
    existing = await db.scalar(
        select(AnalysisJob).where(AnalysisJob.idempotency_key == idempotency_key)
    )
    if existing is not None:
        # Re-queue only if the previous attempt is finished and failed.
        if existing.state == JobState.FAILED and existing.attempts < existing.max_attempts:
            existing.state = JobState.QUEUED
            existing.progress = 0
            existing.stage = "requeued"
            existing.failure_reason = None
            existing.queued_at = datetime.now(UTC)
            await db.flush()
            register_push(db, existing)
            return EnqueuedJob(job=existing, created=False, queued_to_redis=True)
        return EnqueuedJob(job=existing, created=False, queued_to_redis=False)

    settings = get_settings()
    job = AnalysisJob(
        id=uuid.uuid4(),
        job_type=job_type,
        evidence_id=evidence_id,
        inspection_id=inspection_id,
        idempotency_key=idempotency_key,
        state=JobState.QUEUED,
        progress=0,
        stage="queued",
        attempts=0,
        max_attempts=settings.job_max_tries,
        queued_at=datetime.now(UTC),
        payload=payload or {},
        requested_by_id=requested_by_id,
    )
    db.add(job)
    try:
        await db.flush()
    except IntegrityError:
        # Another request created the same job between the check and the insert.
        await db.rollback()
        existing = await db.scalar(
            select(AnalysisJob).where(AnalysisJob.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return EnqueuedJob(job=existing, created=False, queued_to_redis=False)
        raise

    JOBS_ENQUEUED.labels(job_type=job_type).inc()
    register_push(db, job)
    return EnqueuedJob(job=job, created=True, queued_to_redis=True)


#: Session key holding job ids created in this transaction and not yet pushed.
_PENDING_PUSH_KEY = "maanak_pending_job_pushes"


def register_push(db: AsyncSession, job: AnalysisJob) -> None:
    """Note that this job must reach Redis once the transaction commits."""
    pending: list[uuid.UUID] = db.info.setdefault(_PENDING_PUSH_KEY, [])
    if job.id not in pending:
        pending.append(job.id)


async def push_pending(db: AsyncSession) -> int:
    """Push jobs registered during this session, after their rows are committed.

    Each row is re-read first. The read runs in a new transaction, so it sees only
    committed data: if the caller rolled back, the row is absent and the message is
    correctly not sent.
    """
    pending: list[uuid.UUID] = db.info.pop(_PENDING_PUSH_KEY, [])
    sent = 0
    for job_id in pending:
        job = await db.get(AnalysisJob, job_id)
        if job is None:
            logger.info("job_push_skipped_not_committed", job_id=str(job_id))
            continue
        if await _push(job):
            sent += 1
    return sent


async def _push(job: AnalysisJob) -> bool:
    """Push a job to Redis. A failure is logged, not raised.

    The database row already records that work is outstanding, and
    ``requeue_stalled`` recovers it, so a Redis blip must not fail the request that
    accepted the evidence.
    """
    try:
        pool = await get_pool()
        message = await pool.enqueue_job(
            job.job_type,
            str(job.id),
            _job_id=f"maanak:{job.id}",
            _defer_by=timedelta(seconds=0),
        )
    except (RedisError, OSError) as exc:
        logger.warning(
            "job_push_failed", job_id=str(job.id), job_type=job.job_type, error=type(exc).__name__
        )
        return False
    if message is not None:
        job.queue_message_id = str(getattr(message, "job_id", ""))[:120] or None
    return True


async def mark_running(db: AsyncSession, job_id: uuid.UUID) -> AnalysisJob | None:
    job = await db.get(AnalysisJob, job_id)
    if job is None:
        return None
    job.state = JobState.RUNNING
    job.attempts += 1
    job.started_at = datetime.now(UTC)
    job.stage = "started"
    job.progress = 5
    await db.flush()
    return job


async def update_progress(
    db: AsyncSession, job_id: uuid.UUID, *, progress: int, stage: str
) -> None:
    """Write real progress so the interface is not showing a fake spinner."""
    await db.execute(
        update(AnalysisJob)
        .where(AnalysisJob.id == job_id)
        .values(progress=max(0, min(100, progress)), stage=stage[:60])
    )


async def mark_succeeded(db: AsyncSession, job_id: uuid.UUID, *, result: dict[str, Any]) -> None:
    await db.execute(
        update(AnalysisJob)
        .where(AnalysisJob.id == job_id)
        .values(
            state=JobState.SUCCEEDED,
            progress=100,
            stage="complete",
            finished_at=datetime.now(UTC),
            result=result,
            failure_reason=None,
        )
    )


async def mark_failed(
    db: AsyncSession, job_id: uuid.UUID, *, reason: str, retryable: bool = True
) -> None:
    job = await db.get(AnalysisJob, job_id)
    if job is None:
        return
    exhausted = job.attempts >= job.max_attempts or not retryable
    job.state = JobState.FAILED
    job.stage = "failed" if exhausted else "awaiting_retry"
    job.finished_at = datetime.now(UTC) if exhausted else None
    # Reason text is written for an officer to read, not a stack trace.
    job.failure_reason = reason[:2000]
    await db.flush()


async def requeue_stalled(db: AsyncSession, *, older_than_seconds: int = 900) -> int:
    """Re-enqueue jobs left running by a worker that died. Returns the count."""
    threshold = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    stalled = list(
        await db.scalars(
            select(AnalysisJob).where(
                AnalysisJob.state == JobState.RUNNING,
                AnalysisJob.started_at < threshold,
                AnalysisJob.attempts < AnalysisJob.max_attempts,
            )
        )
    )
    for job in stalled:
        job.state = JobState.QUEUED
        job.stage = "requeued_after_stall"
        job.progress = 0
        job.queued_at = datetime.now(UTC)
        await db.flush()
        # Registered rather than pushed: a worker that took the message now would
        # still read the uncommitted RUNNING state and skip the job as already busy.
        register_push(db, job)
    if stalled:
        logger.warning("jobs_requeued_after_stall", count=len(stalled))
    return len(stalled)


async def queue_depth(db: AsyncSession) -> int:
    depth = await db.scalar(
        select(func.count())
        .select_from(AnalysisJob)
        .where(AnalysisJob.state.in_([JobState.QUEUED, JobState.RUNNING]))
    )
    value = int(depth or 0)
    QUEUE_DEPTH.set(value)
    return value


async def job_status(db: AsyncSession, job_id: uuid.UUID) -> dict[str, Any] | None:
    job = await db.get(AnalysisJob, job_id)
    if job is None:
        return None
    return {
        "id": str(job.id),
        "job_type": job.job_type,
        "state": job.state,
        "progress": job.progress,
        "stage": job.stage,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "queued_at": job.queued_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "failure_reason": job.failure_reason,
        "evidence_id": str(job.evidence_id) if job.evidence_id else None,
        "inspection_id": str(job.inspection_id) if job.inspection_id else None,
        "result": job.result,
    }


async def jobs_for_inspection(db: AsyncSession, inspection_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = list(
        await db.scalars(
            select(AnalysisJob)
            .where(AnalysisJob.inspection_id == inspection_id)
            .order_by(AnalysisJob.queued_at.desc())
            .limit(50)
        )
    )
    return [
        {
            "id": str(row.id),
            "job_type": row.job_type,
            "state": row.state,
            "progress": row.progress,
            "stage": row.stage,
            "evidence_id": str(row.evidence_id) if row.evidence_id else None,
            "queued_at": row.queued_at,
            "finished_at": row.finished_at,
            "failure_reason": row.failure_reason,
        }
        for row in rows
    ]


async def evidence_pending_analysis(db: AsyncSession, inspection_id: uuid.UUID) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(Evidence)
            .where(
                Evidence.inspection_id == inspection_id,
                Evidence.analysis_state.in_(
                    [AnalysisState.PENDING, AnalysisState.QUEUED, AnalysisState.RUNNING]
                ),
            )
        )
        or 0
    )
