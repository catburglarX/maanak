"""Public complaints and their attachments."""

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
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..domain.enums import ComplaintCategory, ComplaintState, values
from .base import Base, TimestampMixin, enum_check, uuid_pk, version_column


class Complaint(Base, TimestampMixin):
    """A complaint submitted through the public site.

    The public reference is shown to the submitter. It is not sufficient to read
    the complaint: status lookup also requires the email or phone that was
    supplied, so a guessed reference alone reveals nothing.
    """

    __tablename__ = "complaints"
    __table_args__ = (
        UniqueConstraint("reference", name="uq_complaints_reference"),
        enum_check("state", values(ComplaintState), "complaints_state"),
        enum_check("category", values(ComplaintCategory), "complaints_category"),
        Index("ix_complaints_state_priority", "state", "priority"),
        Index("ix_complaints_jurisdiction", "jurisdiction_code", "state"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[int] = version_column()
    reference: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # --- What the consumer reported ---------------------------------------
    product_name: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    brand: Mapped[str | None] = mapped_column(String(160), index=True)
    barcode_value: Mapped[str | None] = mapped_column(String(32), index=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    purchase_date: Mapped[date | None] = mapped_column(Date)
    seller_name: Mapped[str | None] = mapped_column(String(240))
    marketplace_name: Mapped[str | None] = mapped_column(String(160))
    listing_url: Mapped[str | None] = mapped_column(Text)
    stated_mrp: Mapped[str | None] = mapped_column(String(40))
    stated_price_paid: Mapped[str | None] = mapped_column(String(40))
    location_text: Mapped[str | None] = mapped_column(String(240))

    # --- Contact (optional) ------------------------------------------------
    contact_name: Mapped[str | None] = mapped_column(String(160))
    contact_email: Mapped[str | None] = mapped_column(String(320), index=True)
    contact_phone: Mapped[str | None] = mapped_column(String(40))
    #: SHA-256 of the lowercased email or normalised phone; used for status
    #: lookup without exposing the contact value in a URL.
    contact_lookup_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    consent_to_contact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    privacy_notice_version: Mapped[str | None] = mapped_column(String(20))

    # --- Handling ----------------------------------------------------------
    state: Mapped[str] = mapped_column(
        String(30), nullable=False, default=ComplaintState.RECEIVED, index=True
    )
    #: 0-100. Derived from the documented triage table, not from a model score.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=30, index=True)
    priority_reason: Mapped[str | None] = mapped_column(String(240))
    jurisdiction_code: Mapped[str | None] = mapped_column(String(64), index=True)
    jurisdiction_name: Mapped[str | None] = mapped_column(String(160))
    assigned_officer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    matched_product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL")
    )
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("complaints.id", ondelete="SET NULL")
    )
    triage_note: Mapped[str | None] = mapped_column(Text)
    resolution_note: Mapped[str | None] = mapped_column(Text)
    closure_reason: Mapped[str | None] = mapped_column(String(240))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Submission provenance --------------------------------------------
    submitted_ip: Mapped[str | None] = mapped_column(INET)
    submitted_user_agent: Mapped[str | None] = mapped_column(String(400))
    submission_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    attachments: Mapped[list[ComplaintAttachment]] = relationship(
        back_populates="complaint", cascade="all, delete-orphan", lazy="selectin"
    )

    __mapper_args__ = {"version_id_col": version}


class ComplaintAttachment(Base, TimestampMixin):
    """A file supplied with a complaint: package image or purchase proof."""

    __tablename__ = "complaint_attachments"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_complaint_attachments_storage_key"),
        enum_check(
            "attachment_kind",
            ("package_image", "purchase_proof", "other"),
            "complaint_attachments_attachment_kind",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    complaint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("complaints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attachment_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    detected_mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_bucket: Mapped[str] = mapped_column(String(120), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    #: Set when an officer promotes this attachment into inspection evidence.
    promoted_evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evidence.id", ondelete="SET NULL")
    )

    complaint: Mapped[Complaint] = relationship(back_populates="attachments")
