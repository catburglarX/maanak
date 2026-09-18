"""Tamper-evident audit events.

Each event stores the hash of the previous event in its chain. Altering or
deleting a historical row breaks every subsequent hash, which the verification
endpoint detects. The application role is granted INSERT and SELECT only, so the
API itself cannot rewrite history.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, uuid_pk


class AuditEvent(Base):
    """One material action recorded for accountability."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_entity", "entity_type", "entity_id", "recorded_at"),
        Index("ix_audit_events_actor_time", "actor_id", "recorded_at"),
        Index("ix_audit_events_action_time", "action", "recorded_at"),
        Index("ix_audit_events_chain_sequence", "chain_key", "sequence", unique=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()

    #: Chain partition. "global" for a single chain; kept configurable so that
    #: high-volume deployments can shard by jurisdiction without a schema change.
    chain_key: Mapped[str] = mapped_column(String(64), nullable=False, default="global")
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    #: SHA-256 over the canonical serialisation of this event plus previous_hash.
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column()
    entity_version: Mapped[int | None] = mapped_column(Integer)

    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_role: Mapped[str | None] = mapped_column(String(30))
    actor_email: Mapped[str | None] = mapped_column(String(320))
    jurisdiction_code: Mapped[str | None] = mapped_column(String(64))
    #: True for public actions such as complaint submission.
    actor_is_public: Mapped[bool] = mapped_column(nullable=False, default=False)

    #: Only fields that materially changed, with secrets excluded by the writer.
    old_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    new_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    reason: Mapped[str | None] = mapped_column(Text)

    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(400))

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
