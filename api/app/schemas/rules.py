"""Schemas for rule authoring and governance."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field, model_validator

from ..domain.enums import QuantityKind, RuleStatus
from .common import ExpectedVersion, ReadSchema, Schema

RuleCode = Annotated[
    str,
    Field(min_length=3, max_length=80, pattern=r"^[A-Z][A-Z0-9-]{2,79}$"),
]


class RuleScope(Schema):
    commodity_scope: list[str] = Field(default_factory=list, max_length=50)
    package_scope: list[str] = Field(default_factory=list, max_length=20)
    quantity_kind: QuantityKind | None = None
    #: Inclusive lower bound in SI base units for the dimension.
    quantity_min_base: Decimal | None = Field(default=None, ge=0)
    #: Exclusive upper bound in SI base units for the dimension.
    quantity_max_base: Decimal | None = Field(default=None, gt=0)
    applies_to_imported: bool | None = None
    applies_to_ecommerce: bool | None = None
    applies_to_multipiece: bool | None = None
    exceptions: list[dict[str, str]] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _bounds_ordered(self) -> RuleScope:
        if (
            self.quantity_min_base is not None
            and self.quantity_max_base is not None
            and self.quantity_min_base >= self.quantity_max_base
        ):
            raise ValueError(
                "quantity_min_base must be below quantity_max_base "
                "(the lower bound is inclusive, the upper bound exclusive)"
            )
        return self


class RuleCreateRequest(RuleScope):
    code: RuleCode
    title: str = Field(min_length=6, max_length=240)
    citation: str = Field(min_length=6, max_length=300)
    source_url: str = Field(min_length=8, max_length=2000)
    source_document_title: str | None = Field(default=None, max_length=300)
    gazette_reference: str | None = Field(default=None, max_length=240)
    source_retrieved_on: date | None = None
    effective_from: date
    effective_to: date | None = None
    amends_rule_version_id: str | None = None
    supersedes_rule_version_id: str | None = None
    required_inputs: list[str] = Field(default_factory=list, max_length=20)
    test_kind: str = Field(min_length=3, max_length=60)
    test_specification: dict[str, Any] = Field(default_factory=dict)
    plain_explanation: str = Field(min_length=20, max_length=4000)
    interpretation_note: str = Field(
        min_length=20,
        max_length=4000,
        description="How the legal text was read to produce this test. Required for review.",
    )
    uncertainty_note: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _window_ordered(self) -> RuleCreateRequest:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to cannot be before effective_from")
        return self


class RuleUpdateRequest(RuleScope):
    title: str | None = Field(default=None, min_length=6, max_length=240)
    citation: str | None = Field(default=None, min_length=6, max_length=300)
    source_url: str | None = Field(default=None, min_length=8, max_length=2000)
    source_document_title: str | None = Field(default=None, max_length=300)
    gazette_reference: str | None = Field(default=None, max_length=240)
    source_retrieved_on: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    required_inputs: list[str] | None = Field(default=None, max_length=20)
    test_kind: str | None = Field(default=None, min_length=3, max_length=60)
    test_specification: dict[str, Any] | None = None
    plain_explanation: str | None = Field(default=None, min_length=20, max_length=4000)
    interpretation_note: str | None = Field(default=None, min_length=20, max_length=4000)
    uncertainty_note: str | None = Field(default=None, max_length=4000)
    expected_version: ExpectedVersion


class ScenarioInput(Schema):
    name: str = Field(min_length=3, max_length=160)
    kind: str = Field(min_length=3, max_length=40)
    expected_outcome: str = Field(min_length=3, max_length=40)
    values: dict[str, Any] = Field(default_factory=dict)
    label_seen_without_value: dict[str, bool] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    evidence_complete: bool = True
    missing_faces: list[str] = Field(default_factory=list, max_length=20)
    inspection_date: date | None = None
    commodity_category: str | None = Field(default=None, max_length=120)
    package_type: str | None = Field(default=None, max_length=40)
    quantity_kind: str | None = Field(default=None, max_length=20)
    quantity_base: str | None = Field(default=None, max_length=40)
    is_imported: bool = False
    is_ecommerce: bool = False
    is_multipiece: bool = False
    claimed_exceptions: list[str] = Field(default_factory=list, max_length=20)


class SimulateRequest(Schema):
    scenarios: list[ScenarioInput] = Field(default_factory=list, max_length=60)
    #: Use the scenarios shipped with a seeded rule, if any.
    use_seeded_scenarios: bool = True
    #: Add the mechanically generated date-window and boundary cases.
    include_generated: bool = True


class StatusChangeRequest(Schema):
    target_status: RuleStatus
    note: str | None = Field(default=None, max_length=4000)
    expected_version: ExpectedVersion


class LegalAuthorityRequest(Schema):
    confirmed: bool
    note: str = Field(
        min_length=20,
        max_length=4000,
        description="Who confirmed the interpretation, on what basis, and when.",
    )
    expected_version: ExpectedVersion


class RuleSummary(ReadSchema):
    id: str
    record_version: int
    code: str
    version: int
    title: str
    citation: str
    status: str
    effective_from: date
    effective_to: date | None
    test_kind: str
    legal_authority_confirmed: bool
    effective_for_inspections: bool
    updated_at: datetime


class RuleDetail(RuleSummary):
    source_url: str
    source_document_title: str | None
    gazette_reference: str | None
    source_retrieved_on: date | None
    commodity_scope: list[Any] = Field(default_factory=list)
    package_scope: list[Any] = Field(default_factory=list)
    quantity_kind: str | None
    quantity_min_base: Decimal | None
    quantity_max_base: Decimal | None
    applies_to_imported: bool | None
    applies_to_ecommerce: bool | None
    applies_to_multipiece: bool | None
    exceptions: list[Any] = Field(default_factory=list)
    required_inputs: list[Any] = Field(default_factory=list)
    test_specification: dict[str, Any] = Field(default_factory=dict)
    plain_explanation: str
    interpretation_note: str
    uncertainty_note: str | None
    legal_authority_note: str | None
    author_id: str | None
    reviewer_id: str | None
    approver_id: str | None
    submitted_at: datetime | None
    reviewed_at: datetime | None
    approved_at: datetime | None
    withdrawn_at: datetime | None
    withdrawn_reason: str | None
    governance: dict[str, Any] = Field(default_factory=dict)
    reviews: list[dict[str, Any]] = Field(default_factory=list)
    test_results: dict[str, Any] = Field(default_factory=dict)


class SimulationResponse(ReadSchema):
    passed_count: int
    failed_count: int
    all_passed: bool
    approval_ready: bool
    covered_kinds: list[str] = Field(default_factory=list)
    required_kinds: list[str] = Field(default_factory=list)
    missing_kinds: list[str] = Field(default_factory=list)
    scenarios: list[dict[str, Any]] = Field(default_factory=list)


class CheckKindInfo(ReadSchema):
    kind: str
    summary: str
