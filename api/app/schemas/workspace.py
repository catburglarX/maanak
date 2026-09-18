"""Schemas for the inspection workspace."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field, field_validator

from ..domain.enums import (
    DeclarationType,
    FaceCaptureState,
    InspectionSource,
    PackageFace,
    PackageType,
    QuantityKind,
    ReviewState,
)
from .common import ExpectedVersion, ReadSchema, Schema

Reason = Annotated[str, Field(min_length=5, max_length=2000)]


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------
class IdentifierInput(Schema):
    scheme: str = Field(default="gtin", pattern="^(gtin|ean8|upca|itf14|isbn|internal|other)$")
    value: str = Field(min_length=4, max_length=32)
    officer_confirmed: bool = False


class ResponsiblePartyInput(Schema):
    party_role: str = Field(pattern="^(manufacturer|packer|importer|marketer|brand_owner)$")
    legal_name: str = Field(min_length=2, max_length=240)
    address_line: str | None = Field(default=None, max_length=1000)
    locality: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=80)
    pin_code: str | None = Field(default=None, max_length=12)
    country: str | None = Field(default=None, max_length=80)
    consumer_care_name: str | None = Field(default=None, max_length=160)
    consumer_care_email: str | None = Field(default=None, max_length=320)
    consumer_care_phone: str | None = Field(default=None, max_length=40)


class ProductCreateRequest(Schema):
    brand: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=240)
    common_generic_name: str | None = Field(default=None, max_length=240)
    commodity_category: str = Field(min_length=2, max_length=120)
    is_food: bool = False
    package_type: PackageType = PackageType.OTHER
    quantity_kind: QuantityKind | None = None
    declared_net_quantity: Decimal | None = Field(default=None, gt=0, le=Decimal("1000000"))
    declared_net_quantity_unit: str | None = Field(default=None, max_length=16)
    is_imported: bool = False
    country_of_origin: str | None = Field(default=None, max_length=80)
    is_multipiece: bool = False
    pieces_per_package: int | None = Field(default=None, ge=1, le=10000)
    notes: str | None = Field(default=None, max_length=2000)
    identifiers: list[IdentifierInput] = Field(default_factory=list, max_length=6)
    responsible_parties: list[ResponsiblePartyInput] = Field(default_factory=list, max_length=6)


class ProductUpdateRequest(Schema):
    brand: str | None = Field(default=None, min_length=1, max_length=160)
    name: str | None = Field(default=None, min_length=1, max_length=240)
    common_generic_name: str | None = Field(default=None, max_length=240)
    commodity_category: str | None = Field(default=None, min_length=2, max_length=120)
    is_food: bool | None = None
    package_type: PackageType | None = None
    quantity_kind: QuantityKind | None = None
    declared_net_quantity: Decimal | None = Field(default=None, gt=0, le=Decimal("1000000"))
    declared_net_quantity_unit: str | None = Field(default=None, max_length=16)
    is_imported: bool | None = None
    country_of_origin: str | None = Field(default=None, max_length=80)
    is_multipiece: bool | None = None
    pieces_per_package: int | None = Field(default=None, ge=1, le=10000)
    notes: str | None = Field(default=None, max_length=2000)
    expected_version: ExpectedVersion


class IdentifierOut(ReadSchema):
    id: str
    scheme: str
    value: str
    formatted: str | None = None
    check_digit_valid: bool | None
    barcode_symbology: str | None
    is_primary: bool
    officer_confirmed: bool
    source: str


class ResponsiblePartyOut(ReadSchema):
    id: str
    party_role: str
    legal_name: str
    address_line: str | None
    locality: str | None
    state: str | None
    pin_code: str | None
    country: str | None
    consumer_care_name: str | None
    consumer_care_email: str | None
    consumer_care_phone: str | None


class ProductSummary(ReadSchema):
    id: str
    version: int
    brand: str
    name: str
    common_generic_name: str | None
    commodity_category: str
    is_food: bool
    package_type: str
    quantity_kind: str | None
    declared_net_quantity: Decimal | None
    declared_net_quantity_unit: str | None
    net_quantity_base: Decimal | None
    is_imported: bool
    country_of_origin: str | None
    is_multipiece: bool
    pieces_per_package: int | None
    primary_gtin: str | None
    inspection_count: int = 0
    updated_at: datetime


class ProductDetail(ProductSummary):
    notes: str | None
    identifiers: list[IdentifierOut] = Field(default_factory=list)
    responsible_parties: list[ResponsiblePartyOut] = Field(default_factory=list)


class GtinCheckResponse(ReadSchema):
    input: str
    acceptable: bool
    normalised: str | None
    length: int | None
    scheme: str | None
    check_digit_valid: bool
    problem: str | None
    india_gs1_prefix: bool
    note: str


# --------------------------------------------------------------------------
# Inspections
# --------------------------------------------------------------------------
class InspectionCreateRequest(Schema):
    #: Supply one of these. A new product is created only when product_id is absent.
    product_id: str | None = None
    product: ProductCreateRequest | None = None

    inspection_date: date
    source: InspectionSource = InspectionSource.FIELD_INSPECTION
    jurisdiction_code: str | None = Field(
        default=None,
        max_length=64,
        description="Defaults to the officer's own jurisdiction.",
    )
    jurisdiction_name: str | None = Field(default=None, max_length=160)
    premises_name: str | None = Field(default=None, max_length=240)
    premises_address: str | None = Field(default=None, max_length=1000)
    marketplace_name: str | None = Field(default=None, max_length=160)
    listing_url: str | None = Field(default=None, max_length=2000)
    batch_reference: str | None = Field(default=None, max_length=60)
    complaint_id: str | None = None

    @field_validator("inspection_date")
    @classmethod
    def _not_future(cls, value: date) -> date:
        if value > date.today():
            raise ValueError("the inspection date cannot be in the future")
        return value


class InspectionUpdateRequest(Schema):
    premises_name: str | None = Field(default=None, max_length=240)
    premises_address: str | None = Field(default=None, max_length=1000)
    marketplace_name: str | None = Field(default=None, max_length=160)
    listing_url: str | None = Field(default=None, max_length=2000)
    batch_reference: str | None = Field(default=None, max_length=60)
    assigned_officer_id: str | None = None
    expected_version: ExpectedVersion


class FaceStateRequest(Schema):
    face: PackageFace
    capture_state: FaceCaptureState
    reason: str | None = Field(default=None, max_length=2000)


class DecisionRequest(Schema):
    decision: str = Field(pattern="^(compliant|violation_found|unable_to_determine)$")
    note: str = Field(
        min_length=20,
        max_length=5000,
        description="Reasoned note explaining the decision. Printed in the report.",
    )
    expected_version: ExpectedVersion


class CharacterHeightMeasurement(Schema):
    declaration: DeclarationType
    observed_mm: Decimal = Field(gt=0, le=Decimal("500"))
    uncertainty_mm: Decimal = Field(default=Decimal("0.2"), ge=0, le=Decimal("10"))
    method: str = Field(
        min_length=3,
        max_length=120,
        description="How the measurement was taken, for example 'steel rule' or "
        "'calibrated against a 10 mm reference in the photograph'.",
    )


class MeasurementRequest(Schema):
    measurements: list[CharacterHeightMeasurement] = Field(min_length=1, max_length=20)
    expected_version: ExpectedVersion


class InspectionSummary(ReadSchema):
    id: str
    version: int
    reference: str
    state: str
    decision: str | None
    jurisdiction_code: str
    jurisdiction_name: str
    source: str
    inspection_date: date
    premises_name: str | None
    product: ProductSummary
    evidence_count: int
    pending_candidate_count: int
    current_finding_count: int
    created_at: datetime
    updated_at: datetime


class EvidenceSummary(ReadSchema):
    id: str
    face: str
    sequence: int
    original_filename: str
    detected_mime_type: str
    size_bytes: int
    sha256: str
    original_width: int | None
    original_height: int | None
    analysis_state: str
    analysis_failure_reason: str | None
    quality_verdict: str | None
    quality_actions: list[str] = Field(default_factory=list)
    quality_override_reason: str | None
    barcodes: list[Any] = Field(default_factory=list)
    uploaded_by_id: str | None
    server_received_at: datetime
    view_url: str | None = None
    thumbnail_url: str | None = None


class CandidateOut(ReadSchema):
    id: str
    version: int
    evidence_id: str | None
    declaration_type: str
    machine_state: str
    review_state: str
    matched_text: str | None
    context_text: str | None
    normalised_value: dict[str, Any] = Field(default_factory=dict)
    display_value: str | None = None
    unit: str | None
    region: list[Any] = Field(default_factory=list)
    text_height_px: float | None
    machine_confidence: float | None
    machine_explanation: str | None
    parser_name: str
    corrected_value: dict[str, Any] = Field(default_factory=dict)
    correction_reason: str | None
    review_note: str | None
    reviewed_by_id: str | None
    reviewed_at: datetime | None
    is_usable_for_rules: bool


class CandidateReviewRequest(Schema):
    review_state: ReviewState
    #: Required when review_state is "corrected". Free text as printed on the package;
    #: the server parses it with the same parser used for OCR output.
    corrected_text: str | None = Field(default=None, max_length=2000)
    correction_reason: str | None = Field(default=None, max_length=2000)
    review_note: str | None = Field(default=None, max_length=2000)
    expected_version: ExpectedVersion


class FindingOut(ReadSchema):
    id: str
    version: int
    declaration_type: str | None
    outcome: str
    effective_outcome: str
    explanation: str
    expected_value: str | None
    observed_value: str | None
    calculation: list[Any] = Field(default_factory=list)
    test_inputs: dict[str, Any] = Field(default_factory=dict)
    selection_reason: dict[str, Any] = Field(default_factory=dict)
    rule: dict[str, Any] = Field(default_factory=dict)
    candidate_id: str | None
    officer_outcome: str | None
    officer_note: str | None
    engine_version: str
    is_current: bool


class FindingOverrideRequest(Schema):
    officer_outcome: str = Field(
        pattern="^(compliant|non_compliant|unable_to_determine|not_applicable|additional_evidence_required)$"
    )
    officer_note: Reason
    expected_version: ExpectedVersion


class InspectionDetail(InspectionSummary):
    decision_note: str | None
    decided_at: datetime | None
    decided_by_id: str | None
    assigned_officer_id: str | None
    reviewer_id: str | None
    checks_executed_at: datetime | None
    rules_evaluated_count: int
    listing_url: str | None
    marketplace_name: str | None
    batch_reference: str | None
    premises_address: str | None
    context: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceSummary] = Field(default_factory=list)
    candidates: list[CandidateOut] = Field(default_factory=list)
    findings: list[FindingOut] = Field(default_factory=list)
    findings_summary: dict[str, Any] = Field(default_factory=dict)
    available_transitions: list[dict[str, Any]] = Field(default_factory=list)
    jobs: list[dict[str, Any]] = Field(default_factory=list)
    reports: list[dict[str, Any]] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)


class RunChecksResponse(ReadSchema):
    findings_written: int
    rules_considered: int
    rules_applied: int
    outcomes: dict[str, int] = Field(default_factory=dict)
    unreviewed_candidates: int
    notes: list[str] = Field(default_factory=list)
    selected_rules: list[dict[str, Any]] = Field(default_factory=list)
    rejected_rules: list[dict[str, Any]] = Field(default_factory=list)


class EvidenceUploadResponse(ReadSchema):
    evidence: EvidenceSummary
    quality: dict[str, Any]
    job_id: str | None
    queued: bool
    message: str
