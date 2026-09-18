"""Inspections, their package-face checklist and their transition history."""

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

from ..domain.enums import (
    FaceCaptureState,
    InspectionSource,
    InspectionState,
    PackageFace,
    values,
)
from .base import Base, TimestampMixin, enum_check, uuid_pk, version_column


class Inspection(Base, TimestampMixin):
    """One inspection of one package variant by one officer.

    ``version`` gives optimistic locking: two reviewers opening the same
    inspection cannot silently overwrite each other's decision.
    """

    __tablename__ = "inspections"
    __table_args__ = (
        enum_check("state", values(InspectionState), "inspections_state"),
        enum_check("source", values(InspectionSource), "inspections_source"),
        Index("ix_inspections_jurisdiction_state", "jurisdiction_code", "state"),
        Index("ix_inspections_state_updated", "state", "updated_at"),
        Index("ix_inspections_assigned", "assigned_officer_id", "state"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[int] = version_column()
    reference: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)

    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(
        String(40), nullable=False, default=InspectionState.DRAFT, index=True
    )

    jurisdiction_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    jurisdiction_name: Mapped[str] = mapped_column(String(160), nullable=False)

    created_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    assigned_officer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    #: Where the package was inspected.
    source: Mapped[str] = mapped_column(
        String(40), nullable=False, default=InspectionSource.FIELD_INSPECTION
    )
    #: The legally relevant date: rule selection uses this, not the record date.
    inspection_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    premises_name: Mapped[str | None] = mapped_column(String(240))
    premises_address: Mapped[str | None] = mapped_column(Text)
    marketplace_name: Mapped[str | None] = mapped_column(String(160))
    listing_url: Mapped[str | None] = mapped_column(Text)
    batch_reference: Mapped[str | None] = mapped_column(String(60))

    complaint_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("complaints.id", ondelete="SET NULL"), index=True
    )
    parent_inspection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("inspections.id", ondelete="SET NULL")
    )

    #: Free-form context captured in the field, validated by schema at the edge.
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    decision: Mapped[str | None] = mapped_column(String(40))
    decision_note: Mapped[str | None] = mapped_column(Text)
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    checks_executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rules_evaluated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retention_state: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    faces: Mapped[list[InspectionFace]] = relationship(
        back_populates="inspection", cascade="all, delete-orphan", lazy="selectin"
    )

    __mapper_args__ = {"version_id_col": version}

    @property
    def is_frozen(self) -> bool:
        from ..domain.enums import FROZEN_STATES

        return self.state in FROZEN_STATES


class InspectionFace(Base, TimestampMixin):
    """The capture checklist.

    Each expected package face carries an explicit state, and a reason is required
    when a face is not captured. This is what makes "no MRP image" different from
    "MRP absent from the package".
    """

    __tablename__ = "inspection_faces"
    __table_args__ = (
        UniqueConstraint("inspection_id", "face", name="uq_inspection_faces_inspection_id_face"),
        enum_check("face", values(PackageFace), "inspection_faces_face"),
        enum_check("capture_state", values(FaceCaptureState), "inspection_faces_capture_state"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    inspection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inspections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    face: Mapped[str] = mapped_column(String(40), nullable=False)
    capture_state: Mapped[str] = mapped_column(
        String(40), nullable=False, default=FaceCaptureState.REQUIRED
    )
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reason: Mapped[str | None] = mapped_column(Text)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    inspection: Mapped[Inspection] = relationship(back_populates="faces")


class StateTransition(Base):
    """Append-only record of every state change in the system.

    Used by inspections, complaints and cases so that one timeline query can show
    the whole life of a matter.
    """

    __tablename__ = "state_transitions"
    __table_args__ = (
        Index("ix_state_transitions_entity", "entity_type", "entity_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(40))
    to_state: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_role: Mapped[str | None] = mapped_column(String(30))
    jurisdiction_code: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(64))
    entity_version: Mapped[int | None] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
