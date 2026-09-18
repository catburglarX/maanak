"""Evidence files, their derivatives, OCR output and analysis jobs.

Chain of custody rule: the ``evidence`` row plus the object in the originals
bucket are never modified after creation. Everything the pipeline produces is a
derivative or a separate row, so the original bytes and their hash stay
reproducible.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..domain.enums import (
    AnalysisState,
    EvidenceKind,
    JobState,
    PackageFace,
    RetentionState,
    values,
)
from .base import Base, TimestampMixin, enum_check, uuid_pk


class Evidence(Base, TimestampMixin):
    """One original evidence file with its full chain-of-custody record."""

    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_evidence_storage_key"),
        enum_check("face", values(PackageFace), "evidence_face"),
        enum_check("analysis_state", values(AnalysisState), "evidence_analysis_state"),
        enum_check("retention_state", values(RetentionState), "evidence_retention_state"),
        Index("ix_evidence_inspection_face", "inspection_id", "face"),
        Index("ix_evidence_sha256", "sha256"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    inspection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inspections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    complaint_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("complaints.id", ondelete="SET NULL"), index=True
    )
    face: Mapped[str] = mapped_column(String(40), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # --- File identity ----------------------------------------------------
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    supplied_mime_type: Mapped[str | None] = mapped_column(String(120))
    #: Determined from the file's own bytes, never trusted from the client.
    detected_mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    hash_algorithm: Mapped[str] = mapped_column(String(20), nullable=False, default="sha256")
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    original_width: Mapped[int | None] = mapped_column(Integer)
    original_height: Mapped[int | None] = mapped_column(Integer)
    stored_width: Mapped[int | None] = mapped_column(Integer)
    stored_height: Mapped[int | None] = mapped_column(Integer)

    # --- Storage ----------------------------------------------------------
    storage_bucket: Mapped[str] = mapped_column(String(120), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    storage_version_id: Mapped[str | None] = mapped_column(String(120))
    storage_encryption: Mapped[str | None] = mapped_column(String(40))

    # --- Custody ----------------------------------------------------------
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    #: Set for evidence attached to a public complaint by an anonymous submitter.
    uploaded_by_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    upload_client_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    server_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    upload_ip: Mapped[str | None] = mapped_column(INET)
    device_make: Mapped[str | None] = mapped_column(String(80))
    device_model: Mapped[str | None] = mapped_column(String(120))
    #: Only populated when the officer explicitly permits location capture.
    capture_latitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    capture_longitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    capture_accuracy_m: Mapped[float | None] = mapped_column(Float)
    #: "preserved_in_record" - EXIF is read into exif_summary and the original
    #: bytes are stored unchanged; derivatives are stripped of EXIF.
    exif_handling: Mapped[str] = mapped_column(
        String(40), nullable=False, default="preserved_in_record"
    )
    exif_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    # --- Analysis ---------------------------------------------------------
    analysis_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AnalysisState.PENDING, index=True
    )
    analysis_failure_reason: Mapped[str | None] = mapped_column(Text)
    quality: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    quality_accepted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    quality_override_reason: Mapped[str | None] = mapped_column(Text)
    barcodes: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)

    malware_scan_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="not_scanned"
    )
    malware_scan_detail: Mapped[str | None] = mapped_column(String(240))

    # --- Retention --------------------------------------------------------
    retention_state: Mapped[str] = mapped_column(
        String(30), nullable=False, default=RetentionState.ACTIVE
    )
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    derivatives: Mapped[list[EvidenceDerivative]] = relationship(
        back_populates="evidence", cascade="all, delete-orphan", lazy="selectin"
    )
    ocr_results: Mapped[list[OcrResult]] = relationship(
        back_populates="evidence", cascade="all, delete-orphan"
    )


class EvidenceDerivative(Base, TimestampMixin):
    """A regenerable image produced from an original (thumbnail, OCR input, ...)."""

    __tablename__ = "evidence_derivatives"
    __table_args__ = (
        UniqueConstraint("evidence_id", "kind", name="uq_evidence_derivatives_evidence_id_kind"),
        enum_check("kind", values(EvidenceKind), "evidence_derivatives_kind"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(120), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    #: How the derivative was produced, so it can be regenerated identically.
    transform: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    generator_version: Mapped[str] = mapped_column(String(40), nullable=False, default="1")

    evidence: Mapped[Evidence] = relationship(back_populates="derivatives")


class OcrResult(Base, TimestampMixin):
    """Raw OCR output for one evidence file under one OCR profile.

    Raw text and word boxes are retained so that a later parser change can be
    re-run without re-reading the image, and so an officer can see exactly what
    the machine read.
    """

    __tablename__ = "ocr_results"
    __table_args__ = (
        UniqueConstraint("evidence_id", "profile", name="uq_ocr_results_evidence_id_profile"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile: Mapped[str] = mapped_column(String(40), nullable=False, default="default")
    engine: Mapped[str] = mapped_column(String(40), nullable=False)
    engine_version: Mapped[str | None] = mapped_column(String(40))
    languages: Mapped[str] = mapped_column(String(40), nullable=False)
    detected_script: Mapped[str | None] = mapped_column(String(40))
    orientation_applied_degrees: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: [{text, confidence, box:[x1,y1,x2,y2], line, block, height_px}, ...]
    words: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    #: [{text, box, words:[i,...], mean_confidence}, ...]
    lines: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    mean_confidence: Mapped[float | None] = mapped_column(Float)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    evidence: Mapped[Evidence] = relationship(back_populates="ocr_results")


class AnalysisJob(Base, TimestampMixin):
    """Queue record for background analysis.

    ``idempotency_key`` makes re-enqueueing the same work harmless, which is what
    prevents duplicate candidates after a retry.
    """

    __tablename__ = "analysis_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_analysis_jobs_idempotency_key"),
        enum_check("state", values(JobState), "analysis_jobs_state"),
        Index("ix_analysis_jobs_state_created", "state", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    job_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evidence.id", ondelete="CASCADE"), index=True
    )
    inspection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("inspections.id", ondelete="CASCADE"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default=JobState.QUEUED, index=True
    )
    #: 0-100, written by the worker so the UI can show real progress.
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stage: Mapped[str | None] = mapped_column(String(60))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    queue_message_id: Mapped[str | None] = mapped_column(String(120))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
