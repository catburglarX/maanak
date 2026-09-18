"""Cases, notices and case timeline events.

An issued notice is frozen: its rendered text and the template version used are
stored on the notice row, so editing a template later cannot change what was
already served.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..domain.enums import CaseState, DeliveryMethod, NoticeType, values
from .base import Base, TimestampMixin, enum_check, uuid_pk, version_column


class Case(Base, TimestampMixin):
    """A matter opened against a responsible party following an inspection."""

    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("reference", name="uq_cases_reference"),
        enum_check("state", values(CaseState), "cases_state"),
        Index("ix_cases_jurisdiction_state", "jurisdiction_code", "state"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[int] = version_column()
    reference: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    inspection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inspections.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reports.id", ondelete="SET NULL")
    )
    state: Mapped[str] = mapped_column(
        String(40), nullable=False, default=CaseState.DRAFT, index=True
    )

    jurisdiction_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    jurisdiction_name: Mapped[str] = mapped_column(String(160), nullable=False)

    respondent_name: Mapped[str] = mapped_column(String(240), nullable=False)
    respondent_role: Mapped[str | None] = mapped_column(String(30))
    respondent_address: Mapped[str | None] = mapped_column(Text)
    respondent_email: Mapped[str | None] = mapped_column(String(320))
    respondent_phone: Mapped[str | None] = mapped_column(String(40))

    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Statutory provisions relied on, copied from the cited rule versions.
    legal_basis: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    rule_version_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)

    opened_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    assigned_officer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    follow_up_inspection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("inspections.id", ondelete="SET NULL")
    )

    outcome: Mapped[str | None] = mapped_column(String(60))
    outcome_note: Mapped[str | None] = mapped_column(Text)
    closure_reason: Mapped[str | None] = mapped_column(String(240))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    notices: Mapped[list[Notice]] = relationship(
        back_populates="case", cascade="all, delete-orphan", lazy="selectin"
    )

    __mapper_args__ = {"version_id_col": version}


class Notice(Base, TimestampMixin):
    """A notice prepared and possibly issued under a case."""

    __tablename__ = "notices"
    __table_args__ = (
        UniqueConstraint("reference", name="uq_notices_reference"),
        enum_check("notice_type", values(NoticeType), "notices_notice_type"),
        enum_check("delivery_method", values(DeliveryMethod), "notices_delivery_method"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reference: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    notice_type: Mapped[str] = mapped_column(String(40), nullable=False)

    template_code: Mapped[str] = mapped_column(String(60), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Rendered text captured at issue time. Never regenerated.
    rendered_body: Mapped[str] = mapped_column(Text, nullable=False)
    rendered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    body_sha256: Mapped[str | None] = mapped_column(String(64))

    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issued_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    is_frozen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    delivery_method: Mapped[str | None] = mapped_column(String(30))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_proof_reference: Mapped[str | None] = mapped_column(String(160))
    delivery_note: Mapped[str | None] = mapped_column(Text)

    response_due_on: Mapped[date | None] = mapped_column(Date, index=True)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_summary: Mapped[str | None] = mapped_column(Text)
    response_storage_key: Mapped[str | None] = mapped_column(Text)

    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_reason: Mapped[str | None] = mapped_column(Text)

    case: Mapped[Case] = relationship(back_populates="notices")


class CaseEvent(Base):
    """Timeline entry for a case: hearing, reminder, follow-up, note."""

    __tablename__ = "case_events"
    __table_args__ = (Index("ix_case_events_case_time", "case_id", "occurred_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NoticeTemplate(Base, TimestampMixin):
    """Versioned notice template. New versions never alter issued notices."""

    __tablename__ = "notice_templates"
    __table_args__ = (
        UniqueConstraint("code", "version", name="uq_notice_templates_code_version"),
        enum_check("notice_type", values(NoticeType), "notice_templates_notice_type"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    notice_type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: Placeholder names the template expects, validated before rendering.
    placeholders: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
