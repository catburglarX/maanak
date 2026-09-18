"""Audit trail.

Every material action appends one row to ``audit_events``. Each row carries the
hash of its predecessor, so the chain can be replayed and any alteration or
removal of a historical row is detectable.

Appends are serialised with a PostgreSQL transaction-level advisory lock. Without
it, two concurrent requests could read the same ``previous_hash`` and produce a
fork. The lock is held only for the insert and is released when the transaction
ends.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditEvent
from .canonical import canonical_sha256

GLOBAL_CHAIN = "global"
GENESIS_HASH = "0" * 64

#: Field names never written into an audit payload, even if a caller passes them.
REDACTED_KEYS = frozenset(
    {
        "password",
        "new_password",
        "current_password",
        "password_hash",
        "token",
        "access_token",
        "refresh_token",
        "refresh_token_hash",
        "token_hash",
        "mfa_secret",
        "secret",
        "jwt_secret",
        "s3_secret_key",
        "signature_value",
        "authorization",
        "cookie",
        "set-cookie",
    }
)
REDACTED_PLACEHOLDER = "[redacted]"


@dataclass
class AuditContext:
    """Who is acting and through which request."""

    actor_id: uuid.UUID | None = None
    actor_role: str | None = None
    actor_email: str | None = None
    jurisdiction_code: str | None = None
    request_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    is_public: bool = False

    @classmethod
    def public(
        cls, *, request_id: str | None, ip_address: str | None, user_agent: str | None
    ) -> AuditContext:
        return cls(
            request_id=request_id,
            ip_address=ip_address,
            user_agent=user_agent,
            is_public=True,
        )


@dataclass
class AuditRecord:
    """A prepared audit entry."""

    action: str
    entity_type: str
    entity_id: uuid.UUID | None = None
    entity_version: int | None = None
    old_values: dict[str, Any] = field(default_factory=dict)
    new_values: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None


def redact(values: dict[str, Any] | None) -> dict[str, Any]:
    """Drop sensitive keys, recursively, before anything is persisted."""
    if not values:
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in values.items():
        if key.lower() in REDACTED_KEYS:
            cleaned[key] = REDACTED_PLACEHOLDER
        elif isinstance(value, dict):
            cleaned[key] = redact(value)
        elif isinstance(value, list):
            cleaned[key] = [redact(item) if isinstance(item, dict) else item for item in value]
        else:
            cleaned[key] = value
    return cleaned


def compute_event_hash(
    *,
    chain_key: str,
    sequence: int,
    previous_hash: str,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    entity_version: int | None,
    actor_id: uuid.UUID | None,
    actor_role: str | None,
    jurisdiction_code: str | None,
    old_values: dict[str, Any],
    new_values: dict[str, Any],
    reason: str | None,
    recorded_at: datetime,
) -> str:
    """Hash the fields that constitute the event's meaning.

    Deliberately excludes ``request_id``, ``ip_address`` and ``user_agent``:
    those are useful for investigation but are transport details, and including
    them would make the chain depend on values a proxy can change.
    """
    return canonical_sha256(
        {
            "chain_key": chain_key,
            "sequence": sequence,
            "previous_hash": previous_hash,
            "action": action,
            "entity_type": entity_type,
            "entity_id": str(entity_id) if entity_id else None,
            "entity_version": entity_version,
            "actor_id": str(actor_id) if actor_id else None,
            "actor_role": actor_role,
            "jurisdiction_code": jurisdiction_code,
            "old_values": old_values,
            "new_values": new_values,
            "reason": reason,
            "recorded_at": recorded_at,
        }
    )


async def _acquire_chain_lock(db: AsyncSession, chain_key: str) -> None:
    """Serialise appends to one chain for the rest of this transaction."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"maanak_audit_chain:{chain_key}"},
    )


async def append(
    db: AsyncSession,
    context: AuditContext,
    record: AuditRecord,
    *,
    chain_key: str = GLOBAL_CHAIN,
) -> AuditEvent:
    """Append one event. The caller commits.

    The row is flushed so that a failure surfaces inside the caller's transaction
    rather than at commit time.
    """
    await _acquire_chain_lock(db, chain_key)

    tail = (
        await db.execute(
            select(AuditEvent.sequence, AuditEvent.event_hash)
            .where(AuditEvent.chain_key == chain_key)
            .order_by(AuditEvent.sequence.desc())
            .limit(1)
        )
    ).first()

    sequence = (tail.sequence + 1) if tail else 1
    previous_hash = tail.event_hash if tail else GENESIS_HASH

    old_values = redact(record.old_values)
    new_values = redact(record.new_values)
    recorded_at = datetime.now(UTC)

    event = AuditEvent(
        id=uuid.uuid4(),
        chain_key=chain_key,
        sequence=sequence,
        previous_hash=previous_hash,
        event_hash=compute_event_hash(
            chain_key=chain_key,
            sequence=sequence,
            previous_hash=previous_hash,
            action=record.action,
            entity_type=record.entity_type,
            entity_id=record.entity_id,
            entity_version=record.entity_version,
            actor_id=context.actor_id,
            actor_role=context.actor_role,
            jurisdiction_code=context.jurisdiction_code,
            old_values=old_values,
            new_values=new_values,
            reason=record.reason,
            recorded_at=recorded_at,
        ),
        action=record.action,
        entity_type=record.entity_type,
        entity_id=record.entity_id,
        entity_version=record.entity_version,
        actor_id=context.actor_id,
        actor_role=context.actor_role,
        actor_email=context.actor_email,
        jurisdiction_code=context.jurisdiction_code,
        actor_is_public=context.is_public,
        old_values=old_values,
        new_values=new_values,
        reason=record.reason,
        request_id=context.request_id,
        ip_address=context.ip_address,
        user_agent=(context.user_agent or "")[:400] or None,
        recorded_at=recorded_at,
    )
    db.add(event)
    await db.flush()
    return event


async def record(
    db: AsyncSession,
    context: AuditContext,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    *,
    entity_version: int | None = None,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
    reason: str | None = None,
) -> AuditEvent:
    """Convenience wrapper around :func:`append`."""
    return await append(
        db,
        context,
        AuditRecord(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_version=entity_version,
            old_values=old_values or {},
            new_values=new_values or {},
            reason=reason,
        ),
    )


@dataclass
class ChainVerification:
    """Result of replaying a chain."""

    chain_key: str
    events_checked: int
    intact: bool
    first_broken_sequence: int | None = None
    problem: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain_key": self.chain_key,
            "events_checked": self.events_checked,
            "intact": self.intact,
            "first_broken_sequence": self.first_broken_sequence,
            "problem": self.problem,
        }


async def verify_chain(
    db: AsyncSession, *, chain_key: str = GLOBAL_CHAIN, limit: int | None = None
) -> ChainVerification:
    """Recompute every hash and confirm the links.

    Detects three kinds of tampering: an altered field (hash mismatch), a removed
    row (sequence gap) and a rewritten link (previous_hash mismatch).
    """
    statement = (
        select(AuditEvent)
        .where(AuditEvent.chain_key == chain_key)
        .order_by(AuditEvent.sequence.asc())
    )
    if limit:
        statement = statement.limit(limit)

    events = list(await db.scalars(statement))
    expected_previous = GENESIS_HASH
    expected_sequence = 1

    for event in events:
        if event.sequence != expected_sequence:
            return ChainVerification(
                chain_key=chain_key,
                events_checked=expected_sequence - 1,
                intact=False,
                first_broken_sequence=event.sequence,
                problem=(
                    f"sequence gap: expected {expected_sequence}, found {event.sequence}. "
                    "An event was removed or inserted out of order."
                ),
            )
        if (event.previous_hash or GENESIS_HASH) != expected_previous:
            return ChainVerification(
                chain_key=chain_key,
                events_checked=expected_sequence - 1,
                intact=False,
                first_broken_sequence=event.sequence,
                problem="previous_hash does not match the preceding event.",
            )

        recomputed = compute_event_hash(
            chain_key=event.chain_key,
            sequence=event.sequence,
            previous_hash=event.previous_hash or GENESIS_HASH,
            action=event.action,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            entity_version=event.entity_version,
            actor_id=event.actor_id,
            actor_role=event.actor_role,
            jurisdiction_code=event.jurisdiction_code,
            old_values=event.old_values or {},
            new_values=event.new_values or {},
            reason=event.reason,
            recorded_at=event.recorded_at,
        )
        if recomputed != event.event_hash:
            return ChainVerification(
                chain_key=chain_key,
                events_checked=expected_sequence - 1,
                intact=False,
                first_broken_sequence=event.sequence,
                problem="recorded event_hash does not match the event contents.",
            )

        expected_previous = event.event_hash
        expected_sequence += 1

    return ChainVerification(chain_key=chain_key, events_checked=len(events), intact=True)


async def chain_summary(db: AsyncSession, *, chain_key: str = GLOBAL_CHAIN) -> dict[str, Any]:
    """Cheap chain status for the audit page header."""
    total = await db.scalar(
        select(func.count()).select_from(AuditEvent).where(AuditEvent.chain_key == chain_key)
    )
    tail = (
        await db.execute(
            select(AuditEvent.sequence, AuditEvent.event_hash, AuditEvent.recorded_at)
            .where(AuditEvent.chain_key == chain_key)
            .order_by(AuditEvent.sequence.desc())
            .limit(1)
        )
    ).first()
    return {
        "chain_key": chain_key,
        "event_count": int(total or 0),
        "head_sequence": tail.sequence if tail else 0,
        "head_hash": tail.event_hash if tail else None,
        "last_event_at": tail.recorded_at if tail else None,
    }
