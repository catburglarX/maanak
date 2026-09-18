"""Declaration candidates and legal findings.

Two distinct records, deliberately not merged:

``DeclarationCandidate``
    What the machine read from an image, plus what an officer decided about that
    reading. Holds machine state and review state.

``Finding``
    The result of applying one approved rule version to reviewed evidence. Holds
    the legal outcome, the rule version cited, and the calculation performed.

Keeping them apart is what stops "OCR found nothing" from becoming "the package
is non-compliant".
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..domain.enums import (
    DeclarationType,
    LegalOutcome,
    MachineState,
    ReviewState,
    values,
)
from .base import Base, TimestampMixin, enum_check, uuid_pk, version_column


class DeclarationCandidate(Base, TimestampMixin):
    """One machine reading of one declaration, with its officer review."""

    __tablename__ = "declaration_candidates"
    __table_args__ = (
        enum_check(
            "declaration_type", values(DeclarationType), "declaration_candidates_declaration_type"
        ),
        enum_check("machine_state", values(MachineState), "declaration_candidates_machine_state"),
        enum_check("review_state", values(ReviewState), "declaration_candidates_review_state"),
        Index("ix_declaration_candidates_inspection_type", "inspection_id", "declaration_type"),
        Index("ix_declaration_candidates_review", "inspection_id", "review_state"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[int] = version_column()

    inspection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inspections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evidence.id", ondelete="SET NULL"), index=True
    )
    ocr_result_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ocr_results.id", ondelete="SET NULL")
    )

    declaration_type: Mapped[str] = mapped_column(String(40), nullable=False)
    machine_state: Mapped[str] = mapped_column(String(30), nullable=False)

    # --- What the machine read -------------------------------------------
    #: Exact OCR substring the parser matched, kept verbatim for inspection.
    matched_text: Mapped[str | None] = mapped_column(Text)
    #: Wider text around the match, for context in the review screen.
    context_text: Mapped[str | None] = mapped_column(Text)
    #: Parsed value in canonical form: {"kind": "money", "amount": "45.00", ...}
    normalised_value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Numeric form for direct comparison and indexing where applicable.
    numeric_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    unit: Mapped[str | None] = mapped_column(String(16))
    #: [x1, y1, x2, y2] in original-image pixel coordinates.
    region: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    #: Measured glyph height in pixels, input to the character-height check.
    text_height_px: Mapped[float | None] = mapped_column(Float)
    machine_confidence: Mapped[float | None] = mapped_column(Float)
    parser_name: Mapped[str] = mapped_column(String(60), nullable=False, default="regex_v1")
    parser_version: Mapped[str] = mapped_column(String(20), nullable=False, default="1")
    machine_explanation: Mapped[str | None] = mapped_column(Text)

    # --- What the officer decided -----------------------------------------
    review_state: Mapped[str] = mapped_column(
        String(30), nullable=False, default=ReviewState.PENDING, index=True
    )
    corrected_value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    corrected_numeric_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    corrected_unit: Mapped[str | None] = mapped_column(String(16))
    correction_reason: Mapped[str | None] = mapped_column(Text)
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __mapper_args__ = {"version_id_col": version}

    @property
    def effective_value(self) -> dict[str, Any]:
        """The value the rule engine must use: officer correction wins."""
        if self.review_state == ReviewState.CORRECTED and self.corrected_value:
            return self.corrected_value
        return self.normalised_value

    @property
    def effective_numeric(self) -> Decimal | None:
        if self.review_state == ReviewState.CORRECTED:
            return self.corrected_numeric_value
        return self.numeric_value

    @property
    def effective_unit(self) -> str | None:
        if self.review_state == ReviewState.CORRECTED:
            return self.corrected_unit or self.unit
        return self.unit

    @property
    def is_reviewed(self) -> bool:
        return self.review_state != ReviewState.PENDING

    @property
    def is_usable_for_rules(self) -> bool:
        """Only confirmed or corrected readings may support a legal conclusion."""
        return self.review_state in {ReviewState.CONFIRMED, ReviewState.CORRECTED}


class CandidateRevision(Base):
    """Immutable history of every change to a candidate.

    An officer correction never destroys the machine reading: the previous state
    is written here first.
    """

    __tablename__ = "candidate_revisions"
    __table_args__ = (Index("ix_candidate_revisions_candidate", "candidate_id", "recorded_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("declaration_candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    new_state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    change_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_role: Mapped[str | None] = mapped_column(String(30))
    request_id: Mapped[str | None] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Finding(Base, TimestampMixin):
    """Outcome of one rule test on one inspection.

    Every row cites exactly one approved rule version and records the inputs and
    the calculation, so the conclusion can be re-derived and defended.
    """

    __tablename__ = "findings"
    __table_args__ = (
        enum_check("outcome", values(LegalOutcome), "findings_outcome"),
        Index("ix_findings_inspection_outcome", "inspection_id", "outcome"),
        Index("ix_findings_rule_version", "rule_version_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[int] = version_column()

    inspection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inspections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rule_versions.id", ondelete="RESTRICT"), nullable=False
    )
    #: Candidate that supplied the primary input, when there was one.
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("declaration_candidates.id", ondelete="SET NULL")
    )
    declaration_type: Mapped[str | None] = mapped_column(String(40), index=True)

    outcome: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    #: Plain-language statement shown to the officer and printed in the report.
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    #: Why this rule version applied: date, commodity, quantity, import status.
    selection_reason: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Inputs the test consumed, recorded verbatim.
    test_inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Ordered arithmetic steps, each a string, so the maths is auditable.
    calculation: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    expected_value: Mapped[str | None] = mapped_column(String(240))
    observed_value: Mapped[str | None] = mapped_column(String(240))
    engine_version: Mapped[str] = mapped_column(String(20), nullable=False, default="1")

    #: Set when a reviewer overrides the deterministic outcome.
    officer_outcome: Mapped[str | None] = mapped_column(String(40))
    officer_note: Mapped[str | None] = mapped_column(Text)
    officer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    officer_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("findings.id", ondelete="SET NULL")
    )

    candidate: Mapped[DeclarationCandidate | None] = relationship(lazy="joined")

    __mapper_args__ = {"version_id_col": version}

    @property
    def effective_outcome(self) -> str:
        return self.officer_outcome or self.outcome
