"""Versioned legal rules and their governance records.

A rule version is a legal interpretation. It carries its citation, its source,
its effective window, its scope, a deterministic test specification, and the
identities of the author, reviewer and approver. Only ``approved`` or ``active``
versions may influence an inspection.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..domain.enums import RuleStatus, values
from .base import Base, TimestampMixin, enum_check, uuid_pk, version_column


class RuleVersion(Base, TimestampMixin):
    """One version of one rule."""

    __tablename__ = "rule_versions"
    __table_args__ = (
        UniqueConstraint("code", "version", name="uq_rule_versions_code_version"),
        enum_check("status", values(RuleStatus), "rule_versions_status"),
        Index("ix_rule_versions_code_status", "code", "status"),
        Index("ix_rule_versions_effective", "effective_from", "effective_to"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    record_version: Mapped[int] = version_column()

    code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    #: Exact statutory citation, for example "Rule 6(1)(e), LMPC Rules 2011".
    citation: Mapped[str] = mapped_column(String(300), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_document_title: Mapped[str | None] = mapped_column(String(300))
    gazette_reference: Mapped[str | None] = mapped_column(String(240))
    source_retrieved_on: Mapped[date | None] = mapped_column(Date)

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    amends_rule_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("rule_versions.id", ondelete="SET NULL")
    )
    supersedes_rule_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("rule_versions.id", ondelete="SET NULL")
    )

    # --- Scope ------------------------------------------------------------
    #: Commodity categories this rule applies to; empty list means all.
    commodity_scope: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    package_scope: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    quantity_kind: Mapped[str | None] = mapped_column(String(20))
    #: Inclusive lower bound in the SI base unit for the dimension.
    quantity_min_base: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    #: Exclusive upper bound in the SI base unit for the dimension.
    quantity_max_base: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    applies_to_imported: Mapped[bool | None] = mapped_column(Boolean)
    applies_to_ecommerce: Mapped[bool | None] = mapped_column(Boolean)
    applies_to_multipiece: Mapped[bool | None] = mapped_column(Boolean)
    exceptions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    transition_conditions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    # --- Test -------------------------------------------------------------
    #: Declaration types the test needs. Missing inputs give
    #: "unable to determine", never "non-compliant".
    required_inputs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    #: Which deterministic check implementation runs, plus its parameters.
    test_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    test_specification: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    plain_explanation: Mapped[str] = mapped_column(Text, nullable=False)
    #: How the legal text was read to produce this test. Required for review.
    interpretation_note: Mapped[str] = mapped_column(Text, nullable=False)
    uncertainty_note: Mapped[str | None] = mapped_column(Text)

    # --- Governance -------------------------------------------------------
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=RuleStatus.DRAFT, index=True
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_reason: Mapped[str | None] = mapped_column(Text)
    #: Set false until a qualified legal authority signs off the interpretation.
    legal_authority_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    legal_authority_note: Mapped[str | None] = mapped_column(Text)
    #: Summary of the simulator run required before approval.
    test_results: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    reviews: Mapped[list[RuleReview]] = relationship(
        back_populates="rule_version", cascade="all, delete-orphan", lazy="selectin"
    )

    __mapper_args__ = {"version_id_col": record_version}

    @property
    def is_effective_for_rules(self) -> bool:
        return self.status in {RuleStatus.APPROVED, RuleStatus.ACTIVE}

    def covers_date(self, on: date) -> bool:
        if on < self.effective_from:
            return False
        return not (self.effective_to is not None and on > self.effective_to)


class RuleReview(Base):
    """A maker-checker decision on a rule version."""

    __tablename__ = "rule_reviews"
    __table_args__ = (
        enum_check(
            "decision",
            ("submitted", "changes_required", "approved", "withdrawn", "activated"),
            "rule_reviews_decision",
        ),
        Index("ix_rule_reviews_rule_version", "rule_version_id", "recorded_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rule_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_role: Mapped[str | None] = mapped_column(String(30))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    rule_version: Mapped[RuleVersion] = relationship(back_populates="reviews")


class RuleTestRun(Base):
    """Result of running the rule simulator against a rule version.

    Approval is blocked until a run covering the mandatory scenarios passes.
    """

    __tablename__ = "rule_test_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rule_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: [{name, inputs, expected, actual, passed}, ...]
    scenarios: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    passed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    all_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    covered_scenario_kinds: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    engine_version: Mapped[str] = mapped_column(String(20), nullable=False, default="1")
    run_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
