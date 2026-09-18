"""Report snapshots and generated documents.

A report is an immutable snapshot. At issue time the entire inspection state
(product, evidence hashes, candidates, findings, rule versions, decision) is
serialised into ``snapshot`` and hashed. Documents are rendered from the snapshot,
never from live tables, so a later edit elsewhere cannot change an issued report.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
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

from ..domain.enums import ReportState, values
from .base import Base, TimestampMixin, enum_check, uuid_pk


class Report(Base, TimestampMixin):
    """An issued inspection report."""

    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint("reference", name="uq_reports_reference"),
        UniqueConstraint("inspection_id", "revision", name="uq_reports_inspection_id_revision"),
        enum_check("state", values(ReportState), "reports_state"),
        Index("ix_reports_snapshot_sha256", "snapshot_sha256"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    reference: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    inspection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inspections.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ReportState.ISSUED, index=True
    )

    #: The frozen record. Everything printed comes from here.
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: SHA-256 over the canonical JSON serialisation of ``snapshot``.
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_algorithm: Mapped[str] = mapped_column(
        String(60), nullable=False, default="sha256/json-canonical-v1"
    )

    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    issued_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    issuing_workspace: Mapped[str] = mapped_column(String(200), nullable=False)
    jurisdiction_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    #: "none" or "development". A real signer is an adapter, never fabricated.
    signature_status: Mapped[str] = mapped_column(String(30), nullable=False, default="none")
    signature_value: Mapped[str | None] = mapped_column(Text)
    signature_key_id: Mapped[str | None] = mapped_column(String(80))
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    verification_code: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)

    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    withdrawn_reason: Mapped[str | None] = mapped_column(Text)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reports.id", ondelete="SET NULL")
    )

    documents: Mapped[list[ReportDocument]] = relationship(
        back_populates="report", cascade="all, delete-orphan", lazy="selectin"
    )


class ReportDocument(Base, TimestampMixin):
    """A rendered PDF or DOCX for a report, with its own hash."""

    __tablename__ = "report_documents"
    __table_args__ = (
        UniqueConstraint("report_id", "format", name="uq_report_documents_report_id_format"),
        enum_check("format", ("pdf", "docx"), "report_documents_format"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    report_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    format: Mapped[str] = mapped_column(String(10), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(120), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    renderer_version: Mapped[str] = mapped_column(String(20), nullable=False, default="1")

    report: Mapped[Report] = relationship(back_populates="documents")
